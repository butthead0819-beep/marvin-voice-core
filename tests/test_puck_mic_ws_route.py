"""/mic_ws 路由只在 MARVIN_PUCK_MIC=1 時註冊（見 car_http_app.build_text_app 尾端）。

build_text_app 是純 wiring、不起 server（見其 docstring），直接檢查
app.router 有沒有這條路由即可，不用真的連線。
"""
from unittest.mock import MagicMock


def _make_vc():
    vc = MagicMock()
    return vc


def _has_mic_ws_route(app) -> bool:
    return any(r.resource is not None and r.resource.canonical == "/mic_ws" for r in app.router.routes())


def test_mic_ws_route_absent_by_default(monkeypatch):
    monkeypatch.delenv("MARVIN_PUCK_MIC", raising=False)
    from car_http_app import build_text_app
    app = build_text_app(_make_vc())
    assert _has_mic_ws_route(app) is False


def test_mic_ws_route_present_when_enabled(monkeypatch):
    monkeypatch.setenv("MARVIN_PUCK_MIC", "1")
    from car_http_app import build_text_app
    app = build_text_app(_make_vc())
    assert _has_mic_ws_route(app) is True


# ── 真 WebSocket 走 handle_mic_ws：binary → feed_adpcm、ptt 文字 → force_wake、token gate ──

class _FakeSession:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.fed = []
        self.ptt = []
        self.closed = False
        _FakeSession.instances.append(self)

    async def feed_adpcm(self, data):
        self.fed.append(bytes(data))

    async def force_wake(self, src):
        self.ptt.append(src)

    async def close(self):
        self.closed = True


async def _ws_roundtrip(monkeypatch, path, *, send=None):
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer
    import puck_mic_session

    monkeypatch.setenv("MARVIN_PUCK_MIC", "1")
    monkeypatch.setenv("MARVIN_PUCK_WAKE_ENGINE", "swift")   # 不需要 whisper_model
    monkeypatch.setattr(puck_mic_session, "PuckMicSession", _FakeSession)
    _FakeSession.instances = []
    from car_http_app import build_text_app
    app = build_text_app(_make_vc(), token="tok", car_devices={"puck-abc123": "狗與露"})
    async with TestClient(TestServer(app)) as client:
        if send is None:
            resp = await client.get(path)
            return resp.status
        ws = await client.ws_connect(path)
        for kind, payload in send:
            if kind == "bin":
                await ws.send_bytes(payload)
            else:
                await ws.send_str(payload)
        await asyncio.sleep(0.1)
        await ws.close()
        await asyncio.sleep(0.1)
        return _FakeSession.instances


import pytest


@pytest.mark.asyncio
async def test_mic_ws_routes_binary_and_ptt(monkeypatch):
    sessions = await _ws_roundtrip(monkeypatch, "/mic_ws?device=puck-abc123&t=tok", send=[
        ("bin", b"\x00" * 260),
        ("str", '{"type":"ptt","src":"pause"}'),
        ("str", '{"type":"ptt"}'),
        ("str", "not json"),
        ("str", '{"type":"other"}'),
    ])
    assert len(sessions) == 1
    s = sessions[0]
    assert s.fed == [b"\x00" * 260]
    assert s.ptt == ["pause", "button"]
    assert s.closed is True
    assert s.kwargs["speaker"] == "狗與露"


@pytest.mark.asyncio
async def test_mic_ws_rejects_missing_token(monkeypatch):
    status = await _ws_roundtrip(monkeypatch, "/mic_ws?device=puck-abc123")
    assert status == 401


@pytest.mark.asyncio
async def test_mic_ws_rejects_unknown_device(monkeypatch):
    status = await _ws_roundtrip(monkeypatch, "/mic_ws?device=puck-zzz&t=tok")
    assert status == 400
