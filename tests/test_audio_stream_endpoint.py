"""
tests/test_audio_stream_endpoint.py

TDD：GET /audio_stream — 車載 puck 連續收音端點（chunked，即時轉送，不緩衝整段）。

驗：
(a) stream_source 有資料 → 200 + chunked body 依序含所有 frame，格式 header 對齊 adapter
(b) 上游 close()（收到 None 哨兵）→ 連線正常結束（不掛住）
(c) stream_source=None（車載模式未接串流輸出）→ 404
(d) 也吃既有 token middleware：無 token → 401
(e) puck 端斷線時 resp.write() 拋 BrokenPipeError（非 ConnectionResetError，車puck實測
    2026-07-25 每次重連都噴一次未接住的 traceback）→ handler 要安靜結束＋照樣 unsubscribe，
    不能讓例外往上炸穿 middleware
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _make_vc():
    vc = MagicMock()
    vc.bot.cogs.get.return_value = None
    return vc


class _FakeStreamSource:
    """subscribe() 回傳預先塞好幀的佇列，模擬 StreamSpeakerOutput 的訂閱介面。"""

    def __init__(self, frames):
        self.rate, self.channels, self.bits = 48000, 2, 16
        self._frames = frames
        self.unsubscribed = []

    def subscribe(self):
        q: asyncio.Queue = asyncio.Queue()
        for f in self._frames:
            q.put_nowait(f)
        return q

    def unsubscribe(self, q):
        self.unsubscribed.append(q)


@pytest.mark.asyncio
async def test_audio_stream_returns_frames_in_order_with_format_headers():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    src = _FakeStreamSource([b"\x01\x02" * 4, b"\x03\x04" * 4, None])
    app = build_text_app(_make_vc(), token="s3cret", stream_source=src)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/audio_stream?t=s3cret")
        assert resp.status == 200
        assert resp.headers["X-Audio-Rate"] == "48000"
        assert resp.headers["X-Audio-Channels"] == "2"
        assert resp.headers["X-Audio-Bits"] == "16"
        body = await resp.read()
        assert body == b"\x01\x02" * 4 + b"\x03\x04" * 4


@pytest.mark.asyncio
async def test_audio_stream_unsubscribes_on_close():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    src = _FakeStreamSource([None])
    app = build_text_app(_make_vc(), token="s3cret", stream_source=src)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/audio_stream?t=s3cret")
        await resp.read()
    assert len(src.unsubscribed) == 1


@pytest.mark.asyncio
async def test_audio_stream_404_when_not_wired():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    app = build_text_app(_make_vc(), token="s3cret")   # 無 stream_source
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/audio_stream?t=s3cret")
        assert resp.status == 404


@pytest.mark.asyncio
async def test_audio_stream_token_gated():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    src = _FakeStreamSource([None])
    app = build_text_app(_make_vc(), token="s3cret", stream_source=src)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/audio_stream")   # 無 token
        assert resp.status == 401


@pytest.mark.asyncio
async def test_audio_stream_swallows_broken_pipe_and_still_unsubscribes(caplog):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    src = _FakeStreamSource([b"\x01\x02" * 4, b"\x03\x04" * 4, None])
    app = build_text_app(_make_vc(), token="s3cret", stream_source=src)
    with patch.object(web.StreamResponse, "write", side_effect=BrokenPipeError()):
        with caplog.at_level("ERROR", logger="aiohttp.server"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/audio_stream?t=s3cret")
                assert resp.status == 200
    assert len(src.unsubscribed) == 1
    assert not any("Error handling request" in r.message for r in caplog.records), (
        "BrokenPipeError 沒被 handler 接住，往上炸穿到 aiohttp.server 噴 traceback"
        "（車puck每次斷線重連都會噴一次，見 handle_audio_stream 的 except 元組）"
    )


@pytest.mark.asyncio
async def test_audio_stream_swallows_wrapped_connection_lost_error(caplog):
    """aiohttp 的 base_protocol._drain_helper 抓到底層 BrokenPipeError 後，實際往外丟的是
    自己包的通用 `ConnectionError("Connection lost")`（不是 BrokenPipeError 的 instance），
    2026-07-25 車puck實測：narrow except (ConnectionResetError, BrokenPipeError) 接不住這個
    包裝過的類別，一樣噴 traceback。"""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    def _raise_wrapped(*_a, **_kw):
        raise ConnectionError("Connection lost") from BrokenPipeError()

    src = _FakeStreamSource([b"\x01\x02" * 4, b"\x03\x04" * 4, None])
    app = build_text_app(_make_vc(), token="s3cret", stream_source=src)
    with patch.object(web.StreamResponse, "write", side_effect=_raise_wrapped):
        with caplog.at_level("ERROR", logger="aiohttp.server"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/audio_stream?t=s3cret")
                assert resp.status == 200
    assert len(src.unsubscribed) == 1
    assert not any("Error handling request" in r.message for r in caplog.records)
