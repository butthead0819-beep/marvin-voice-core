"""PacedSilenceFillQueue：iOS /audio_stream（icy=0）按牆鐘補靜音，換歌空檔不再讓 iPhone 緩衝流失。

背景（2026-10-31 實測）：換歌空檔約 1.35s，mixer 補 1s 靜音後回 b""、Discord AudioPlayer 停下，
剩約 0.35s 完全沒幀；舊的 SilenceFillQueue(timeout_s=1.0) 等不到 1s 不補 → 每次換歌 iPhone
少收 ~0.35s，緩衝一路流失。這裡用真實時間（短）驗證：準時供幀時不補、斷幀時補到跟牆鐘對齊。
"""
from __future__ import annotations

import asyncio
import time

import pytest

FRAME = 3840  # 48k stereo s16 20ms


def _frame(tag: int = 1) -> bytes:
    return bytes([tag]) * FRAME


async def _drain_for(fq, seconds: float) -> list[bytes]:
    out = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            item = await asyncio.wait_for(fq.get(), end - time.monotonic())
        except asyncio.TimeoutError:
            break
        out.append(item)
    return out


@pytest.mark.asyncio
async def test_on_time_frames_get_no_silence():
    from marvin_voice_core.discord_audio_stream_server import PacedSilenceFillQueue

    q: asyncio.Queue = asyncio.Queue()
    fq = PacedSilenceFillQueue(q, frame_bytes=FRAME, max_lag_s=0.25)

    async def producer():
        start = time.monotonic()
        for i in range(25):  # 0.5s 準時供幀
            await asyncio.sleep(max(0.0, start + i * 0.02 - time.monotonic()))
            q.put_nowait(_frame())

    task = asyncio.create_task(producer())
    got = await _drain_for(fq, 0.52)
    await task
    assert all(b == _frame() for b in got), "準時供幀時不該插入靜音"
    assert len(got) >= 20


@pytest.mark.asyncio
async def test_gap_is_filled_up_to_wall_clock():
    from marvin_voice_core.discord_audio_stream_server import PacedSilenceFillQueue

    q: asyncio.Queue = asyncio.Queue()
    for _ in range(10):  # 一次到 0.2s，之後完全斷幀
        q.put_nowait(_frame())
    fq = PacedSilenceFillQueue(q, frame_bytes=FRAME, max_lag_s=0.25)
    t0 = time.monotonic()
    got = await _drain_for(fq, 0.8)
    elapsed = time.monotonic() - t0
    real = [b for b in got if b == _frame()]
    silence = [b for b in got if b != _frame()]
    assert len(real) == 10
    assert silence and all(set(b) == {0} and len(b) % FRAME == 0 for b in silence)
    delivered_s = sum(len(b) for b in got) / FRAME * 0.02
    # 補到落後不超過門檻（0.25s）且不超前牆鐘
    assert elapsed - 0.25 - 0.03 <= delivered_s <= elapsed + 0.03


@pytest.mark.asyncio
async def test_head_start_counts_as_ahead():
    """preroll 塞進佇列的幀是刻意超前的墊，不該被當成「已追平」而在之後少補。"""
    from marvin_voice_core.discord_audio_stream_server import PacedSilenceFillQueue

    q: asyncio.Queue = asyncio.Queue()
    for _ in range(25):  # 0.5s preroll
        q.put_nowait(_frame())
    fq = PacedSilenceFillQueue(q, frame_bytes=FRAME, max_lag_s=0.25, head_start_frames=25)
    t0 = time.monotonic()
    got = await _drain_for(fq, 0.6)
    elapsed = time.monotonic() - t0
    delivered_s = sum(len(b) for b in got) / FRAME * 0.02
    # 期望量＝preroll 0.5s＋牆鐘經過；允許落後到門檻
    assert 0.5 + elapsed - 0.25 - 0.03 <= delivered_s <= 0.5 + elapsed + 0.03


@pytest.mark.asyncio
async def test_none_sentinel_passes_through():
    from marvin_voice_core.discord_audio_stream_server import PacedSilenceFillQueue

    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait(None)
    fq = PacedSilenceFillQueue(q, frame_bytes=FRAME)
    assert await fq.get() is None


def test_ios_uses_paced_queue_others_unchanged():
    from car_http_app import stream_fill_queue
    from marvin_voice_core.discord_audio_stream_server import PacedSilenceFillQueue, SilenceFillQueue

    q: asyncio.Queue = asyncio.Queue()
    assert isinstance(stream_fill_queue({"icy": "0"}, q, preroll=25, frame_bytes=FRAME), PacedSilenceFillQueue)
    other = stream_fill_queue({}, q, preroll=0, frame_bytes=FRAME)
    assert type(other) is SilenceFillQueue


# ---------------------------------------------------------------------------
# 超前時丟純靜音（補了靜音後晚到的幀會讓串流超前牆鐘；2026-10-31 手機實測 1.6h 緩衝漲到 2.3s）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ahead_drops_silence_frames_only_down_to_tolerance():
    from marvin_voice_core.discord_audio_stream_server import PacedSilenceFillQueue

    q: asyncio.Queue = asyncio.Queue()
    for _ in range(40):  # 一次到 0.8s 純靜音（模擬換歌空檔的 grace 靜音晚到成一坨）
        q.put_nowait(b"\x00" * FRAME)
    fq = PacedSilenceFillQueue(q, frame_bytes=FRAME, max_lag_s=0.25)
    t0 = time.monotonic()
    got = await _drain_for(fq, 0.1)
    elapsed = time.monotonic() - t0
    delivered_s = sum(len(b) for b in got) / FRAME * 0.02
    assert delivered_s <= elapsed + 0.25 + 0.03, "超前超過門檻還在送靜音"
    assert delivered_s >= 0.2


@pytest.mark.asyncio
async def test_ahead_never_drops_music():
    from marvin_voice_core.discord_audio_stream_server import PacedSilenceFillQueue

    q: asyncio.Queue = asyncio.Queue()
    for _ in range(40):  # 0.8s 音樂一次到：再超前也一幀都不能丟
        q.put_nowait(_frame())
    fq = PacedSilenceFillQueue(q, frame_bytes=FRAME, max_lag_s=0.25)
    got = await _drain_for(fq, 0.1)
    assert got.count(_frame()) == 40
