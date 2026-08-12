"""
tests/test_puck_deck_endpoint.py

TDD：GET /puck_deck?url=<watch_url> — ESP32 edge端混音單一 deck 原始音源端點。跟
/audio_stream（Mac mixer 已混好的單一輸出）不同，這裡現場 resolve+轉碼「一首歌」，
給 ESP32 開兩條並行連線（deck A/B）各自解碼、自己混（見 main_satellite.py::handle_puck_deck
docstring）。

驗：
(a) puck_command_queue=None（功能未開）→ 404
(b) 無 url 參數 → 400
(c) vc.bot 找不到 MusicCog → 500
(d) MusicCog resolve 失敗（回 None/無 url）→ 502
(e) resolve 成功 → 200 + chunked MP3 body（frame sync 開頭），起了 ffmpeg 子行程轉碼
(f) 也吃既有 token middleware：無 token → 401

ffmpeg/yt-dlp 都是重外部依賴，這裡全部 mock：MusicCog._resolve_yt_query 直接回一個假
stream_url，ffmpeg 子行程用假 PCM bytes 取代真的轉碼輸出，只驗證端點層 wiring。
"""
from __future__ import annotations

import math
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from marvin_voice_core.puck_command_queue import PuckCommandQueue


def _sine_pcm(*, rate: int = 48000, channels: int = 2, seconds: float = 0.05) -> bytes:
    n = int(rate * seconds)
    samples = []
    for i in range(n):
        v = int(3000 * math.sin(2 * math.pi * 440 * i / rate))
        samples.extend([v] * channels)
    return struct.pack("<%dh" % len(samples), *samples)


class _FakeProc:
    def __init__(self, chunks):
        self._chunks = list(chunks) + [b""]
        self.stdout = MagicMock()
        self.stdout.read = AsyncMock(side_effect=self._chunks)
        self.kill = MagicMock()
        self.wait = AsyncMock(return_value=0)


def _make_vc(resolved_url="https://cdn.example.com/stream.m4a", music_cog_present=True):
    vc = MagicMock()
    if not music_cog_present:
        vc.bot.cogs.get.return_value = None
        return vc
    music_cog = MagicMock()
    music_cog._resolve_yt_query = AsyncMock(
        return_value={"url": resolved_url} if resolved_url else None)
    vc.bot.cogs.get.return_value = music_cog
    return vc


@pytest.mark.asyncio
async def test_puck_deck_404_when_not_wired():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    app = build_text_app(_make_vc(), token="s3cret")   # 無 puck_command_queue
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/puck_deck?url=https://youtu.be/a&t=s3cret")
        assert resp.status == 404


@pytest.mark.asyncio
async def test_puck_deck_missing_url_400():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    app = build_text_app(_make_vc(), token="s3cret", puck_command_queue=PuckCommandQueue())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/puck_deck?t=s3cret")
        assert resp.status == 400


@pytest.mark.asyncio
async def test_puck_deck_500_when_music_cog_unavailable():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    vc = _make_vc(music_cog_present=False)
    app = build_text_app(vc, token="s3cret", puck_command_queue=PuckCommandQueue())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/puck_deck?url=https://youtu.be/a&t=s3cret")
        assert resp.status == 500


@pytest.mark.asyncio
async def test_puck_deck_502_when_resolve_fails():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    vc = _make_vc(resolved_url=None)
    app = build_text_app(vc, token="s3cret", puck_command_queue=PuckCommandQueue())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/puck_deck?url=https://youtu.be/a&t=s3cret")
        assert resp.status == 502


@pytest.mark.asyncio
async def test_puck_deck_streams_mp3_on_success():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    vc = _make_vc()
    fake_proc = _FakeProc([_sine_pcm(), _sine_pcm()])
    app = build_text_app(vc, token="s3cret", puck_command_queue=PuckCommandQueue())
    with patch("main_satellite.asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)):
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/puck_deck?url=https://youtu.be/a&t=s3cret")
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "audio/mpeg"
            assert resp.headers["X-Audio-Rate"] == "48000"
            body = await resp.read()
            assert len(body) > 0
            assert body[0] == 0xFF and (body[1] & 0xE0) == 0xE0, "body開頭不是MP3 frame sync"
    fake_proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_puck_deck_seek_param_adds_ffmpeg_ss_before_i():
    """ESP32 端真斷線重連時帶 &seek=<秒數>，ffmpeg 要用 -ss 接回原本位置（放在 -i
    前面才是快速 seek）——見 car_puck.ino::deckNetworkTask 的 deckDownloadedSec。"""
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    vc = _make_vc()
    fake_proc = _FakeProc([_sine_pcm()])
    app = build_text_app(vc, token="s3cret", puck_command_queue=PuckCommandQueue())
    with patch("main_satellite.asyncio.create_subprocess_exec",
               AsyncMock(return_value=fake_proc)) as mock_exec:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/puck_deck?url=https://youtu.be/a&t=s3cret&seek=87.65")
            assert resp.status == 200
            await resp.read()

    args = list(mock_exec.call_args.args)
    assert "-ss" in args
    ss_idx = args.index("-ss")
    assert args[ss_idx + 1] == "87.65"
    i_idx = args.index("-i")
    assert ss_idx < i_idx, "-ss 要在 -i 前面才是快速 seek"


@pytest.mark.asyncio
async def test_puck_deck_no_seek_param_omits_ss():
    """沒帶 seek（新歌開播的正常情況）→ 不該出現 -ss，從頭播。"""
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    vc = _make_vc()
    fake_proc = _FakeProc([_sine_pcm()])
    app = build_text_app(vc, token="s3cret", puck_command_queue=PuckCommandQueue())
    with patch("main_satellite.asyncio.create_subprocess_exec",
               AsyncMock(return_value=fake_proc)) as mock_exec:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/puck_deck?url=https://youtu.be/a&t=s3cret")
            assert resp.status == 200
            await resp.read()

    args = list(mock_exec.call_args.args)
    assert "-ss" not in args


@pytest.mark.asyncio
async def test_puck_deck_seek_zero_omits_ss():
    """seek=0（理論上不該發生，但保守處理）→ 不加 -ss，等同從頭播。"""
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    vc = _make_vc()
    fake_proc = _FakeProc([_sine_pcm()])
    app = build_text_app(vc, token="s3cret", puck_command_queue=PuckCommandQueue())
    with patch("main_satellite.asyncio.create_subprocess_exec",
               AsyncMock(return_value=fake_proc)) as mock_exec:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/puck_deck?url=https://youtu.be/a&t=s3cret&seek=0")
            assert resp.status == 200
            await resp.read()

    args = list(mock_exec.call_args.args)
    assert "-ss" not in args


@pytest.mark.asyncio
async def test_puck_deck_garbage_seek_ignored():
    """seek 帶垃圾值 → 忽略、照樣正常開播，不能讓整個 deck 連不上。"""
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    vc = _make_vc()
    fake_proc = _FakeProc([_sine_pcm()])
    app = build_text_app(vc, token="s3cret", puck_command_queue=PuckCommandQueue())
    with patch("main_satellite.asyncio.create_subprocess_exec",
               AsyncMock(return_value=fake_proc)) as mock_exec:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/puck_deck?url=https://youtu.be/a&t=s3cret&seek=not-a-number")
            assert resp.status == 200
            await resp.read()

    args = list(mock_exec.call_args.args)
    assert "-ss" not in args


@pytest.mark.asyncio
async def test_puck_deck_token_gated():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    app = build_text_app(_make_vc(), token="s3cret", puck_command_queue=PuckCommandQueue())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/puck_deck?url=https://youtu.be/a")   # 無 token
        assert resp.status == 401
