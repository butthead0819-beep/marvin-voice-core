"""marvin_voice_core/discord_audio_stream_server.py：單一 mixer 第 1 刀的 /audio_stream 端點。

驗：
(1) SilenceFillQueue：有幀→回幀；空佇列超時→回 12 幀靜音；None 哨兵原樣回
(2) token gate：設 token 時無 token→401；?t=token→200 且 audio/mpeg
(3) stream_source=None→404
(4) 真 StreamSpeakerOutput 推幀 → body 開頭是 MP3 frame sync
(5) mixer 閒置（沒幀）→ 靜音補位讓 1 秒內讀到非空 MP3 bytes
(6) maybe_start：env 未設→None 且不碰 mixer；env 設成空閒 port→回 server、set_tap 呼叫一次
"""
from __future__ import annotations

import asyncio
import math
import socket
import struct
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

import marvin_voice_core.discord_audio_stream_server as dass
from marvin_voice_core.discord_audio_stream_server import (
    DiscordAudioStreamServer,
    SilenceFillQueue,
    build_app,
    maybe_start,
)
from marvin_voice_core.stream_speaker_output import StreamSpeakerOutput

_FRAME = 3840


def _sine_frame() -> bytes:
    samples = []
    for i in range(_FRAME // 4):
        v = int(3000 * math.sin(2 * math.pi * 440 * i / 48000))
        samples.extend([v, v])
    return struct.pack("<%dh" % len(samples), *samples)


class _PresetSource:
    """subscribe() 回傳預先塞好幀（含 None 結尾）的佇列，模擬 StreamSpeakerOutput 的訂閱介面。"""

    rate, channels, bits = 48000, 2, 16

    def __init__(self):
        self.unsubscribed = []

    def subscribe(self):
        q: asyncio.Queue = asyncio.Queue()
        q.put_nowait(_sine_frame())
        q.put_nowait(_sine_frame())
        q.put_nowait(None)
        return q

    def unsubscribe(self, q):
        self.unsubscribed.append(q)


# ---------- (1) SilenceFillQueue ----------

@pytest.mark.asyncio
async def test_silence_fill_returns_real_frame_when_available():
    q: asyncio.Queue = asyncio.Queue()
    frame = _sine_frame()
    q.put_nowait(frame)
    sfq = SilenceFillQueue(q, timeout_s=0.05)
    assert await sfq.get() == frame


@pytest.mark.asyncio
async def test_silence_fill_returns_silence_on_timeout():
    sfq = SilenceFillQueue(asyncio.Queue(), timeout_s=0.05)
    out = await sfq.get()
    assert out == b"\x00" * (_FRAME * 12)


@pytest.mark.asyncio
async def test_silence_fill_passes_none_sentinel_through():
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait(None)
    sfq = SilenceFillQueue(q, timeout_s=0.05)
    assert await sfq.get() is None


# ---------- (2)(3) HTTP wiring ----------

@pytest.mark.asyncio
async def test_token_gate_rejects_without_token_and_accepts_query_token():
    async with TestClient(TestServer(build_app(_PresetSource(), token="abc"))) as client:
        resp = await client.get("/audio_stream")
        assert resp.status == 401

        resp = await client.get("/audio_stream?t=abc")
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("audio/mpeg")
        await resp.read()


@pytest.mark.asyncio
async def test_stream_source_none_returns_404():
    async with TestClient(TestServer(build_app(None, token=None))) as client:
        resp = await client.get("/audio_stream")
        assert resp.status == 404


# ---------- (4) real StreamSpeakerOutput ----------

@pytest.mark.asyncio
async def test_real_stream_output_emits_mp3_frames():
    loop = asyncio.get_running_loop()
    out = StreamSpeakerOutput(loop)
    async with TestClient(TestServer(build_app(out, token=None))) as client:
        async with client.get("/audio_stream") as resp:
            assert resp.status == 200
            # 等 handler 完成訂閱，再推幀（訂閱前推的幀會丟）
            for _ in range(100):
                if out._subscribers:
                    break
                await asyncio.sleep(0.01)
            frame = _sine_frame()
            for _ in range(50):
                out.write(frame)
                await asyncio.sleep(0)
            out.close()
            body = await asyncio.wait_for(resp.read(), timeout=5)
    assert len(body) > 0
    assert body[0] == 0xFF and (body[1] & 0xE0) == 0xE0


# ---------- (5) mixer 閒置時靜音補位 ----------

@pytest.mark.asyncio
async def test_idle_mixer_still_streams_silence_within_a_second(monkeypatch):
    monkeypatch.setattr(dass, "_SILENCE_FILL_TIMEOUT_S", 0.05)
    loop = asyncio.get_running_loop()
    out = StreamSpeakerOutput(loop)  # 沒有任何 write()＝mixer 閒置
    async with TestClient(TestServer(build_app(out, token=None))) as client:
        async with client.get("/audio_stream") as resp:
            try:
                chunk = await asyncio.wait_for(resp.content.readany(), timeout=1.0)
            finally:
                out.close()  # 結束串流讓 handler 收尾；放 finally：補靜音退化時要「紅」而不是卡死在 teardown
            await asyncio.wait_for(resp.read(), timeout=5)
    assert len(chunk) > 0
    assert chunk[0] == 0xFF and (chunk[1] & 0xE0) == 0xE0


# ---------- (6) maybe_start ----------

@pytest.mark.asyncio
async def test_maybe_start_noop_when_env_unset(monkeypatch):
    monkeypatch.delenv("MARVIN_DISCORD_STREAM_PORT", raising=False)
    vc = MagicMock()
    vc._mixer = MagicMock()
    server = await maybe_start(asyncio.get_running_loop(), vc)
    assert server is None
    vc._mixer.set_tap.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_start_bad_port_degrades_without_touching_mixer(monkeypatch):
    monkeypatch.setenv("MARVIN_DISCORD_STREAM_PORT", "abc")
    vc = MagicMock()
    vc._mixer = MagicMock()
    server = await maybe_start(asyncio.get_running_loop(), vc)  # 不 raise（bot 不能因此起不來）
    assert server is None
    vc._mixer.set_tap.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_start_wires_tap_and_binds_port(monkeypatch):
    monkeypatch.delenv("MARVIN_TEXT_TOKEN", raising=False)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("MARVIN_DISCORD_STREAM_PORT", str(port))
    vc = MagicMock()
    vc._mixer = MagicMock()
    server = await maybe_start(asyncio.get_running_loop(), vc)
    try:
        assert isinstance(server, DiscordAudioStreamServer)
        vc._mixer.set_tap.assert_called_once()
        assert isinstance(vc._mixer.set_tap.call_args.args[0], StreamSpeakerOutput)
    finally:
        if server is not None:
            await server.stop()
