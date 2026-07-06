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
