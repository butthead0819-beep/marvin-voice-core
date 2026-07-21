"""
tests/test_stream_speaker_output.py

TDD：車載 puck streaming 播放 adapter（StreamSpeakerOutput）。先紅後綠。

契約：LocalSpeakerDevice 泵執行緒同步呼叫 write(48k stereo s16 frame)，adapter 經
event loop 即時廣播給所有已訂閱的佇列（給 /audio_stream chunked handler 消化）。

驗：
(a) write N 幀 → 訂閱者依序收到 N 個 frame（不切段、不緩衝整段）
(b) close() 廣播哨兵 None → 訂閱者收到結束訊號
(c) 多訂閱者 fan-out：每個訂閱者都收到同一份幀
(d) unsubscribe 後不再收幀
(e) 慢訂閱者佇列滿 → 該訂閱者丟幀，不炸、不影響其他訂閱者
(f) 空 frame（write(b"")）→ 忽略，不廣播
"""
from __future__ import annotations

import asyncio

import pytest

from marvin_voice_core.stream_speaker_output import StreamSpeakerOutput


async def _settle():
    """讓 call_soon_threadsafe 排的 callback 跑完一輪。"""
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_subscriber_receives_frames_in_order():
    loop = asyncio.get_running_loop()
    out = StreamSpeakerOutput(loop)
    q = out.subscribe()

    out.write(b"\x01\x02" * 480)
    out.write(b"\x03\x04" * 480)
    await _settle()

    assert q.get_nowait() == b"\x01\x02" * 480
    assert q.get_nowait() == b"\x03\x04" * 480


@pytest.mark.asyncio
async def test_close_broadcasts_sentinel():
    loop = asyncio.get_running_loop()
    out = StreamSpeakerOutput(loop)
    q = out.subscribe()

    out.close()
    await _settle()

    assert q.get_nowait() is None


@pytest.mark.asyncio
async def test_multiple_subscribers_all_get_same_frame():
    loop = asyncio.get_running_loop()
    out = StreamSpeakerOutput(loop)
    q1 = out.subscribe()
    q2 = out.subscribe()

    out.write(b"\x05\x06" * 480)
    await _settle()

    assert q1.get_nowait() == b"\x05\x06" * 480
    assert q2.get_nowait() == b"\x05\x06" * 480


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery():
    loop = asyncio.get_running_loop()
    out = StreamSpeakerOutput(loop)
    q = out.subscribe()
    out.unsubscribe(q)

    out.write(b"\x07\x08" * 480)
    await _settle()

    assert q.empty(), "unsubscribe 後不應再收到幀"


@pytest.mark.asyncio
async def test_slow_subscriber_drops_frame_without_crashing_others():
    loop = asyncio.get_running_loop()
    out = StreamSpeakerOutput(loop)
    slow_q = out.subscribe()
    fast_q = out.subscribe()

    for _ in range(slow_q.maxsize):   # 填滿慢訂閱者的佇列
        slow_q.put_nowait(b"x")

    out.write(b"\x09\x0a" * 480)   # 不應對 slow_q 丟例外
    await _settle()

    assert fast_q.get_nowait() == b"\x09\x0a" * 480, "快訂閱者不受慢訂閱者拖累"


@pytest.mark.asyncio
async def test_empty_frame_ignored():
    loop = asyncio.get_running_loop()
    out = StreamSpeakerOutput(loop)
    q = out.subscribe()

    out.write(b"")
    await _settle()

    assert q.empty(), "空 frame 不應廣播"


def test_default_format_matches_mixer_output():
    loop = asyncio.new_event_loop()
    try:
        out = StreamSpeakerOutput(loop)
        assert (out.rate, out.channels, out.bits) == (48000, 2, 16)
    finally:
        loop.close()
