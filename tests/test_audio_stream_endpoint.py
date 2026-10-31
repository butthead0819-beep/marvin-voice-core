"""
tests/test_audio_stream_endpoint.py

TDD：GET /audio_stream — 車載 puck 連續收音端點（chunked，即時轉送，不緩衝整段，MP3編碼）。

驗：
(a) stream_source 有資料 → 200 + chunked body 是合法 MP3（frame sync 開頭），格式 header
    對齊 adapter，Content-Type 標成 audio/mpeg
(b) 上游 close()（收到 None 哨兵）→ 連線正常結束（不掛住）
(c) stream_source=None（車載模式未接串流輸出）→ 404
(d) 也吃既有 token middleware：無 token → 401
(e) puck 端斷線時 resp.write() 拋 BrokenPipeError（非 ConnectionResetError，車puck實測
    2026-07-25 每次重連都噴一次未接住的 traceback）→ handler 要安靜結束＋照樣 unsubscribe，
    不能讓例外往上炸穿 middleware

MP3 編碼本身（壓縮率/frame sync 正確性）由 tests/test_mp3_stream_encoder.py 覆蓋，這裡
只驗證端點層的 wiring（header、生命週期、錯誤處理），frame 內容用真的 sine wave PCM
（而非任意 bytes）餵給端點，確保走過真的 lameenc 也不會炸。
"""
from __future__ import annotations

import asyncio
import math
import struct
from unittest.mock import MagicMock, patch

import pytest


def _make_vc():
    vc = MagicMock()
    vc.bot.cogs.get.return_value = None
    return vc


def _sine_pcm_frame(*, rate: int = 48000, channels: int = 2, seconds: float = 0.05) -> bytes:
    n = int(rate * seconds)
    samples = []
    for i in range(n):
        v = int(3000 * math.sin(2 * math.pi * 440 * i / rate))
        samples.extend([v] * channels)
    return struct.pack("<%dh" % len(samples), *samples)


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
async def test_audio_stream_returns_valid_mp3_with_format_headers():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app

    src = _FakeStreamSource([_sine_pcm_frame(), _sine_pcm_frame(), None])
    app = build_text_app(_make_vc(), token="s3cret", stream_source=src)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/audio_stream?t=s3cret")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "audio/mpeg"
        assert resp.headers["X-Audio-Codec"] == "mp3"
        assert resp.headers["X-Audio-Rate"] == "48000"
        assert resp.headers["X-Audio-Channels"] == "2"
        assert resp.headers["X-Audio-Bits"] == "16"
        body = await resp.read()
        assert len(body) > 0
        assert body[0] == 0xFF and (body[1] & 0xE0) == 0xE0, "body開頭不是MP3 frame sync"


@pytest.mark.asyncio
async def test_audio_stream_unsubscribes_on_close():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app

    src = _FakeStreamSource([None])
    app = build_text_app(_make_vc(), token="s3cret", stream_source=src)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/audio_stream?t=s3cret")
        await resp.read()
    assert len(src.unsubscribed) == 1


@pytest.mark.asyncio
async def test_audio_stream_404_when_not_wired():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app

    app = build_text_app(_make_vc(), token="s3cret")   # 無 stream_source
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/audio_stream?t=s3cret")
        assert resp.status == 404


@pytest.mark.asyncio
async def test_audio_stream_token_gated():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app

    src = _FakeStreamSource([None])
    app = build_text_app(_make_vc(), token="s3cret", stream_source=src)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/audio_stream")   # 無 token
        assert resp.status == 401


@pytest.mark.asyncio
async def test_audio_stream_swallows_broken_pipe_and_still_unsubscribes(caplog):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app

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
    from car_http_app import build_text_app

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


@pytest.mark.asyncio
async def test_audio_stream_without_icy_header_has_no_icy_metaint():
    """既有行為：沒帶 Icy-MetaData header（Pi 的 ffmpeg 不帶）→ 完全不變，無 icy-metaint。"""
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app

    src = _FakeStreamSource([_sine_pcm_frame(), None])
    app = build_text_app(_make_vc(), token="s3cret", stream_source=src)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/audio_stream?t=s3cret")
        assert resp.status == 200
        assert "icy-metaint" not in resp.headers
        await resp.read()


@pytest.mark.asyncio
async def test_audio_stream_with_icy_header_embeds_now_playing_payload():
    """帶 Icy-MetaData: 1 → 有 icy-metaint header，body 能依 ICY 規格拆出以 M1\x1f 開頭的 payload。"""
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    from marvin_voice_core.icy_meta import ICY_METAINT

    # ~1.5s 音訊，128kbps 編碼後遠超過一個 ICY_METAINT（4000 bytes），確保至少跨過一個邊界。
    src = _FakeStreamSource([_sine_pcm_frame(seconds=1.5), None])
    app = build_text_app(_make_vc(), token="s3cret", stream_source=src)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/audio_stream?t=s3cret", headers={"Icy-MetaData": "1"})
        assert resp.status == 200
        assert resp.headers["icy-metaint"] == str(ICY_METAINT)
        body = await resp.read()

    # 依 ICY 規格手動拆：每 ICY_METAINT bytes 音訊後跟一個長度 byte + L*16 bytes metadata。
    i = 0
    n = len(body)
    found_payload = None
    while i < n:
        take = min(ICY_METAINT, n - i)
        i += take
        if i >= n:
            break
        length_byte = body[i]
        i += 1
        if length_byte > 0:
            block = body[i:i + length_byte * 16].rstrip(b"\x00")
            i += length_byte * 16
            found_payload = block
            break
    assert found_payload is not None, "沒有找到任何完整 metadata 區塊（音訊量不足一個 metaint？）"
    assert found_payload.startswith(b"M1\x1f")


# ---------------------------------------------------------------------------
# ?preroll=N：iOS 低延遲模式可選較小的預緩衝；沒帶維持 75 幀（1.5s）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query, expected", [
    ({"icy": "0"}, 75),                          # 沒帶 → 原行為（回歸）
    ({"icy": "0", "preroll": "25"}, 25),         # 照用
    ({"icy": "0", "preroll": "0"}, 0),
    ({"icy": "0", "preroll": "500"}, 75),        # 超出上限 → 夾到 75
    ({"icy": "0", "preroll": "-3"}, 0),          # 負數 → 夾到 0
    ({"icy": "0", "preroll": "abc"}, 75),        # 非整數 → 預設
    ({"preroll": "25"}, 0),                      # 沒 icy=0（puck / Pi）→ 一律不墊
    ({}, 0),
])
def test_stream_preroll_frames(query, expected):
    from car_http_app import stream_preroll_frames
    assert stream_preroll_frames(query) == expected


class _RecordingSource(_FakeStreamSource):
    def __init__(self, frames):
        super().__init__(frames)
        self.prerolls = []

    def subscribe(self, *, preroll_frames: int = 0):
        self.prerolls.append(preroll_frames)
        return super().subscribe()


@pytest.mark.asyncio
async def test_audio_stream_passes_preroll_param_to_subscribe():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app

    src = _RecordingSource([_sine_pcm_frame(), None])
    app = build_text_app(_make_vc(), token="s3cret", stream_source=src)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/audio_stream.mp3?t=s3cret&icy=0&preroll=25")
        await resp.read()
    assert src.prerolls == [25]
