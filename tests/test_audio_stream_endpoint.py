"""
tests/test_audio_stream_endpoint.py

TDD：GET /audio_stream — 車載 puck 連續收音端點（chunked，即時轉送，不緩衝整段）。

驗：
(a) stream_source 有資料 → 200 + chunked body 依序含所有 frame，格式 header 對齊 adapter
(b) 上游 close()（收到 None 哨兵）→ 連線正常結束（不掛住）
(c) stream_source=None（車載模式未接串流輸出）→ 404
(d) 也吃既有 token middleware：無 token → 401
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

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
