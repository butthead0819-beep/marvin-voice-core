"""WyomingSatelliteBridge 測試——假 satellite（真 TCP localhost）+ 假 client，零硬體。

驗：升採樣正確 / 事件流（RunSatellite→Detection→AudioChunk→切句進 callback）/
身分 user_id 傳遞 / Detection hook / send_pcm 播放事件 / 錯格式丟棄不炸。
"""
from __future__ import annotations

import asyncio

import numpy as np
import pytest
from wyoming.audio import AudioChunk, AudioStart
from wyoming.event import async_read_event, async_write_event
from wyoming.wake import Detection

from marvin_voice_core.wyoming_bridge import (
    WyomingSatelliteBridge,
    upsample_16k_mono_to_48k_stereo,
)

pytestmark = pytest.mark.asyncio


# ── 升採樣 ────────────────────────────────────────────────────────────────────

async def test_upsample_length_and_interleave():
    """16k mono N samples → 48k stereo：位元組 ×6、L==R、端點值保留。"""
    mono = np.array([100, 200, 300], dtype=np.int16).tobytes()
    out = upsample_16k_mono_to_48k_stereo(mono)
    assert len(out) == len(mono) * 6  # ×3 升率 ×2 聲道
    s = np.frombuffer(out, dtype=np.int16)
    assert list(s[0::2]) == list(s[1::2])  # L == R
    assert s[0] == 100 and s[-1] == 300    # 端點保留


async def test_upsample_empty_is_empty():
    assert upsample_16k_mono_to_48k_stereo(b"") == b""


# ── 假 satellite（真 TCP）─────────────────────────────────────────────────────

_SPEECH = (np.ones(1600, dtype=np.int16) * 8000).tobytes()  # 100ms @16k mono
_SILENCE = bytes(3200)


async def _fake_satellite(received: list, *, n_speech=10, n_silence=18):
    """localhost 假 satellite：收 run-satellite → 送 Detection + 語音 + 靜默 → 關線。"""
    done = asyncio.Event()

    async def handler(reader, writer):
        evt = await async_read_event(reader)
        received.append(evt.type)
        await async_write_event(Detection(name="mawen").event(), writer)
        for _ in range(n_speech):
            await async_write_event(
                AudioChunk(rate=16000, width=2, channels=1, audio=_SPEECH).event(), writer)
        for _ in range(n_silence):
            await async_write_event(
                AudioChunk(rate=16000, width=2, channels=1, audio=_SILENCE).event(), writer)
        await writer.drain()
        writer.close()
        done.set()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port, done


async def test_bridge_streams_wake_audio_into_cut_callback():
    """整流程：連上→RunSatellite→收 Detection(hook)→語音+1.8s 靜默→切句 callback
    收到 48k stereo 音訊 + 衛星 user_id。"""
    received, cuts, detections = [], [], []

    async def spy(user_id, pcm, ts, *, is_wake_check=False):
        cuts.append((user_id, pcm))

    server, port, done = await _fake_satellite(received)
    bridge = WyomingSatelliteBridge(
        spy, host="127.0.0.1", port=port, user_id="satellite",
        on_detection=lambda name: detections.append(name),
        loop=asyncio.get_running_loop(),
    )
    await asyncio.wait_for(bridge.run(), timeout=10)   # 假衛星關線 → run() 返回
    await asyncio.sleep(0)                              # 讓 cut 的 create_task 跑

    assert received == ["run-satellite"]                # 橋有先送 RunSatellite
    assert detections == ["mawen"]                      # Detection hook 收到
    assert len(cuts) == 1                               # 語音+足量靜默 → 恰一次切句
    user_id, pcm = cuts[0]
    assert user_id == "satellite"
    assert len(pcm) == 10 * len(_SPEECH) * 6            # 全部語音、升採樣 ×6
    server.close()


async def test_bridge_insufficient_silence_no_cut():
    """靜默不足 1.5s（10×100ms=1.0s）→ 不切句（時間基準切句被繼承）。"""
    received, cuts = [], []

    async def spy(user_id, pcm, ts, *, is_wake_check=False):
        cuts.append(pcm)

    server, port, _ = await _fake_satellite(received, n_silence=10)
    bridge = WyomingSatelliteBridge(spy, host="127.0.0.1", port=port,
                                    loop=asyncio.get_running_loop())
    await asyncio.wait_for(bridge.run(), timeout=10)
    await asyncio.sleep(0)
    assert cuts == []
    server.close()


async def test_bridge_drops_unexpected_audio_format():
    """非 16k/mono 音訊 → 丟棄不炸、不進切句。"""
    cuts = []

    async def spy(user_id, pcm, ts, *, is_wake_check=False):
        cuts.append(pcm)

    done = asyncio.Event()

    async def handler(reader, writer):
        await async_read_event(reader)
        bad = AudioChunk(rate=22050, width=2, channels=2, audio=bytes(1024))
        for _ in range(30):
            await async_write_event(bad.event(), writer)
        await writer.drain()
        writer.close()
        done.set()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    bridge = WyomingSatelliteBridge(spy, host="127.0.0.1", port=port,
                                    loop=asyncio.get_running_loop())
    await asyncio.wait_for(bridge.run(), timeout=10)
    await asyncio.sleep(0)
    assert cuts == []
    server.close()


# ── 播放回送 ──────────────────────────────────────────────────────────────────

class _FakeClient:
    def __init__(self):
        self.events = []

    async def write_event(self, event):
        self.events.append(event)


async def test_send_pcm_emits_start_chunks_stop():
    """send_pcm：AudioStart(48k/2ch) → N 個 AudioChunk（≤3840B）→ AudioStop。"""
    async def noop(user_id, pcm, ts, *, is_wake_check=False):
        pass

    bridge = WyomingSatelliteBridge(noop, loop=asyncio.get_running_loop())
    fake = _FakeClient()
    bridge._client = fake

    pcm = bytes(3840 * 2 + 100)  # 2 整塊 + 1 尾塊
    await bridge.send_pcm(pcm)

    types = [e.type for e in fake.events]
    assert types[0] == "audio-start" and types[-1] == "audio-stop"
    chunks = [AudioChunk.from_event(e) for e in fake.events if AudioChunk.is_type(e.type)]
    assert len(chunks) == 3
    assert sum(len(c.audio) for c in chunks) == len(pcm)
    start = AudioStart.from_event(fake.events[0])
    assert (start.rate, start.channels) == (48000, 2)


async def test_send_pcm_no_client_or_empty_is_noop():
    async def noop(user_id, pcm, ts, *, is_wake_check=False):
        pass

    bridge = WyomingSatelliteBridge(noop, loop=asyncio.get_running_loop())
    await bridge.send_pcm(b"\x00\x00")  # _client=None → no-op 不炸
    fake = _FakeClient()
    bridge._client = fake
    await bridge.send_pcm(b"")          # 空 pcm → no-op
    assert fake.events == []
