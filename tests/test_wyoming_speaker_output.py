"""
tests/test_wyoming_speaker_output.py

TDD：衛星播放 adapter（WyomingSpeakerOutput）。先紅後綠。

契約：LocalSpeakerDevice 泵執行緒同步呼叫 write(48k stereo s16 frame)，adapter 經
event loop 把幀轉成 wyoming AudioChunk 送到 bridge._client（衛星 snd-command 播放）。

驗：
(a) write N 幀 → drain 後 client 先收 AudioStart、再收 N 個 AudioChunk（格式 48k/2ch/2B）
(b) close() 送哨兵 None → drain task 結束
(c) client 為 None（未連線）→ 不炸
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from marvin_voice_core.wyoming_speaker_output import WyomingSpeakerOutput


class _FakeClient:
    """記錄 write_event 收到的 wyoming events。"""

    def __init__(self):
        self.events = []

    async def write_event(self, event):
        self.events.append(event)


async def _settle():
    """讓 call_soon_threadsafe 排的 callback + drain task 跑完一輪。"""
    for _ in range(5):
        await asyncio.sleep(0)


# ── (a) write N 幀 → AudioStart + N AudioChunk ────────────────────────────────

@pytest.mark.asyncio
async def test_write_frames_emit_audiostart_then_chunks():
    from wyoming.audio import AudioChunk, AudioStart

    loop = asyncio.get_running_loop()
    client = _FakeClient()
    bridge = SimpleNamespace(_client=client)
    out = WyomingSpeakerOutput(bridge, loop)

    out.write(b"\x01\x02" * 480)   # 一幀
    out.write(b"\x03\x04" * 480)   # 二幀
    await _settle()

    types = [e.type for e in client.events]
    assert AudioStart.is_type(types[0]), f"首個事件應為 AudioStart，實得 {types[0]}"
    chunks = [e for e in client.events if AudioChunk.is_type(e.type)]
    assert len(chunks) == 2, f"應收 2 個 AudioChunk，實得 {len(chunks)}"

    start = AudioStart.from_event(client.events[0])
    assert (start.rate, start.channels, start.width) == (48000, 2, 2)
    first = AudioChunk.from_event(chunks[0])
    assert (first.rate, first.channels, first.width) == (48000, 2, 2)


# ── (b) close() → drain task 結束 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_close_ends_drain_task():
    loop = asyncio.get_running_loop()
    client = _FakeClient()
    bridge = SimpleNamespace(_client=client)
    out = WyomingSpeakerOutput(bridge, loop)

    out.write(b"\x01\x02" * 480)
    await _settle()
    out.close()
    await _settle()

    assert out._task is not None
    assert out._task.done(), "close() 送哨兵後 drain task 應結束"


# ── (c) client 為 None → 不炸、不送任何事件 ──────────────────────────────────

@pytest.mark.asyncio
async def test_no_client_does_not_crash():
    loop = asyncio.get_running_loop()
    bridge = SimpleNamespace(_client=None)
    out = WyomingSpeakerOutput(bridge, loop)

    out.write(b"\x01\x02" * 480)   # 不應丟例外
    await _settle()
    out.close()
    await _settle()
    # 沒有 client 可斷言，能跑到這裡＝沒炸


# ── (d) 靜音幀不送網路（idle 停送，避免 flood 塞爆佇列丟真語音幀）─────────────

@pytest.mark.asyncio
async def test_silence_frame_not_sent():
    from wyoming.audio import AudioChunk

    loop = asyncio.get_running_loop()
    client = _FakeClient()
    bridge = SimpleNamespace(_client=client)
    out = WyomingSpeakerOutput(bridge, loop)

    out.write(b"\x00\x00" * 480)   # 全靜音（abs max 0 < _SILENCE_MAX）
    await _settle()

    chunks = [e for e in client.events if AudioChunk.is_type(e.type)]
    assert len(chunks) == 0, "靜音幀不應送任何 AudioChunk"


# ── (e) 換 client（衛星重連）→ 重送 AudioStart 起新串流 ───────────────────────

@pytest.mark.asyncio
async def test_reconnect_resends_audiostart():
    from wyoming.audio import AudioStart

    loop = asyncio.get_running_loop()
    client_a = _FakeClient()
    bridge = SimpleNamespace(_client=client_a)
    out = WyomingSpeakerOutput(bridge, loop)

    out.write(b"\x01\x02" * 480)
    await _settle()
    assert any(AudioStart.is_type(e.type) for e in client_a.events)

    client_b = _FakeClient()   # 衛星重連→換新 client
    bridge._client = client_b
    out.write(b"\x01\x02" * 480)
    await _settle()

    assert AudioStart.is_type(client_b.events[0].type), \
        "換 client 後首事件應為新 AudioStart（新串流）"


# ── (f) 佇列滿 → 優雅丟幀、不噴例外 ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_queue_full_drops_frame_gracefully():
    loop = asyncio.get_running_loop()
    bridge = SimpleNamespace(_client=None)   # 不消化→佇列會塞滿
    out = WyomingSpeakerOutput(bridge, loop)

    for _ in range(100):            # 填到 maxsize
        out._q.put_nowait(b"x")
    out._enqueue(b"overflow")       # 不應丟 QueueFull


# ── (g) LOWBW 降取樣：48k stereo → 16k mono（純函式，deterministic）──────────

def test_to_16k_mono_downmix_and_downsample():
    from marvin_voice_core.wyoming_speaker_output import _to_16k_mono

    frame = np.full(12, 300, dtype=np.int16).tobytes()   # 6 stereo 樣本、L=R=300
    out = _to_16k_mono(frame)

    arr = np.frombuffer(out, dtype=np.int16)
    assert arr.size == 2, "6 stereo 幀 → downmix 6 mono → /3 降取樣 → 2 樣本"
    assert np.all(arr == 300), "L=R=300 downmix+平均降取樣後仍為 300"
