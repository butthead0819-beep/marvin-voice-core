"""
tests/test_stream_token.py

TDD：串流專用 token（MARVIN_STREAM_TOKEN）——給第三方電台 app 用的唯讀 token。

背景：Funnel 公開整台 server，原本全端點共用 MARVIN_TEXT_TOKEN。把它填進第三方電台 app
等於交出 /say、/play、/gmail_calendar_status 等全部權限。stream_token 只能過 /audio_stream，
外流時最多被人聽串流，且可單獨換掉，不必重燒 ESP32 或重設 Siri 捷徑。

驗：
(a) stream_token 打 /audio_stream（?t= 或 X-Marvin-Token header）→ 200
(b) stream_token 打其他任何端點 → 401
(c) 主 token 照舊能打 /audio_stream 與其他端點
(d) 沒設 stream_token 時，不帶 token / 帶錯 token 打 /audio_stream → 401（不能被 None==None 放行）
(e) 設了 stream_token，帶錯 token 打 /audio_stream → 401
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

MAIN = "main-s3cret"
STREAM = "stream-only"


def _make_vc():
    vc = MagicMock()
    vc.bot.cogs.get.return_value = None
    return vc


class _FakeStreamSource:
    """subscribe() 回傳只有 None 哨兵的佇列＝串流立刻正常結束。"""

    def __init__(self):
        self.rate, self.channels, self.bits = 48000, 2, 16

    def subscribe(self):
        q: asyncio.Queue = asyncio.Queue()
        q.put_nowait(None)
        return q

    def unsubscribe(self, q):
        pass


def _app(*, stream_token=STREAM):
    from car_http_app import build_text_app
    return build_text_app(_make_vc(), token=MAIN, stream_token=stream_token,
                          stream_source=_FakeStreamSource())


@pytest.mark.asyncio
async def test_stream_token_opens_audio_stream_via_query():
    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(_app())) as client:
        resp = await client.get(f"/audio_stream?t={STREAM}")
        assert resp.status == 200
        await resp.read()


@pytest.mark.asyncio
async def test_stream_token_opens_audio_stream_via_header():
    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(_app())) as client:
        resp = await client.get("/audio_stream", headers={"X-Marvin-Token": STREAM})
        assert resp.status == 200
        await resp.read()


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [
    ("POST", "/say"),
    ("GET", "/play?q=x"),
    ("GET", "/now"),
    ("GET", "/gmail_calendar_status"),
    ("GET", "/claude_status"),
    ("POST", "/audio"),
    ("POST", "/car"),
    ("GET", "/hud"),
])
async def test_stream_token_rejected_on_other_endpoints(method, path):
    from aiohttp.test_utils import TestClient, TestServer
    sep = "&" if "?" in path else "?"
    async with TestClient(TestServer(_app())) as client:
        resp = await client.request(method, f"{path}{sep}t={STREAM}", data=b"x")
        assert resp.status == 401, f"{method} {path} 不該被串流 token 打開"


@pytest.mark.asyncio
async def test_main_token_still_opens_audio_stream_and_others():
    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(_app())) as client:
        resp = await client.get(f"/audio_stream?t={MAIN}")
        assert resp.status == 200
        await resp.read()
        resp = await client.get(f"/now?t={MAIN}")
        assert resp.status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "?t=", "?t=wrong"])
async def test_no_stream_token_configured_keeps_audio_stream_gated(query):
    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(_app(stream_token=None))) as client:
        resp = await client.get(f"/audio_stream{query}")
        assert resp.status == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "?t=", "?t=wrong"])
async def test_wrong_token_rejected_on_audio_stream_when_stream_token_set(query):
    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(_app())) as client:
        resp = await client.get(f"/audio_stream{query}")
        assert resp.status == 401
