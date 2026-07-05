"""Tests for LocalMicSink.

TDD: 這些測試先於實作撰寫（先紅），實作完成後全綠。
"""
import asyncio

import numpy as np
import pytest

from protocols import AudioSource

# 20 ms of 48 kHz stereo int16 PCM: 960 frames × 2 ch × 2 bytes = 3840 bytes
FRAME_BYTES = 3840
# int16 amplitude well above any RMS detection threshold (≈ 8000 RMS)
SPEECH_VALUE = 8000


def _speech_frame() -> bytes:
    return (np.ones(FRAME_BYTES // 2, dtype=np.int16) * SPEECH_VALUE).tobytes()


def _silence_frame() -> bytes:
    return bytes(FRAME_BYTES)


@pytest.mark.asyncio
async def test_local_mic_audio_reaches_pipeline_callback():
    """注入語音（6 frames = 23040 bytes > 19200）後接靜音，callback 恰觸發一次且 PCM 非空。"""
    from marvin_voice_core.local_mic_sink import LocalMicSink

    calls: list = []

    async def spy(user_id, pcm, timestamp, *, is_wake_check=False):
        calls.append((user_id, pcm, timestamp))

    # 6 speech frames = 23040 bytes > 19200 min gate, then 25 silence frames to trigger cut
    source = [_speech_frame()] * 6 + [_silence_frame()] * 25
    sink = LocalMicSink(spy, source=source, loop=asyncio.get_running_loop())
    await sink.start()
    await asyncio.sleep(0)

    assert len(calls) == 1
    _, pcm, _ = calls[0]
    assert len(pcm) > 0


@pytest.mark.asyncio
async def test_all_silence_does_not_trigger_callback():
    """全靜音注入不觸發 callback。"""
    from marvin_voice_core.local_mic_sink import LocalMicSink

    calls: list = []

    async def spy(user_id, pcm, timestamp, *, is_wake_check=False):
        calls.append((user_id, pcm, timestamp))

    source = [_silence_frame()] * 30
    sink = LocalMicSink(spy, source=source, loop=asyncio.get_running_loop())
    await sink.start()
    await asyncio.sleep(0)

    assert len(calls) == 0


@pytest.mark.asyncio
async def test_too_short_audio_below_noise_gate_does_not_trigger():
    """語音總量 <= 19200 bytes 時 callback 不觸發（對齊最小音訊大小門檻）。"""
    from marvin_voice_core.local_mic_sink import LocalMicSink

    calls: list = []

    async def spy(user_id, pcm, timestamp, *, is_wake_check=False):
        calls.append((user_id, pcm, timestamp))

    # 4 speech frames × 3840 = 15360 bytes ≤ 19200 — must be dropped by the gate
    source = [_speech_frame()] * 4 + [_silence_frame()] * 25
    sink = LocalMicSink(spy, source=source, loop=asyncio.get_running_loop())
    await sink.start()
    await asyncio.sleep(0)

    assert len(calls) == 0


@pytest.mark.asyncio
async def test_local_mic_sink_satisfies_audiosource_protocol():
    """LocalMicSink 滿足 AudioSource Protocol（isinstance 檢查）。"""
    from marvin_voice_core.local_mic_sink import LocalMicSink

    async def noop(user_id, pcm, ts, *, is_wake_check=False):
        pass

    sink = LocalMicSink(noop, source=[], loop=asyncio.get_running_loop())
    assert isinstance(sink, AudioSource)


def test_mono_to_stereo_doubles_length_and_interleaves():
    """_mono_to_stereo: mono N samples → stereo 2N bytes、L==R、dtype int16。"""
    from marvin_voice_core.local_mic_sink import LocalMicSink

    mono = np.array([1, 2, 3], dtype=np.int16).tobytes()
    stereo = LocalMicSink._mono_to_stereo(mono)

    assert len(stereo) == len(mono) * 2, "stereo 長度應為 mono 的 2 倍"

    samples = np.frombuffer(stereo, dtype=np.int16)
    assert samples.dtype == np.int16
    # interleaved: [L0, R0, L1, R1, ...] — L==R
    assert list(samples[0::2]) == [1, 2, 3], "L channel 應等於 mono 原始值"
    assert list(samples[1::2]) == [1, 2, 3], "R channel 應等於 mono 原始值（L==R）"


# ---------------------------------------------------------------------------
# Active-sink interface tests (TDD — 先紅後綠)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_mic_sink_has_active_sink_interface_attributes():
    """LocalMicSink 具備下游直接存取的全部 active-sink 介面屬性。"""
    from marvin_voice_core.local_mic_sink import LocalMicSink

    async def noop(user_id, pcm, ts, *, is_wake_check=False):
        pass

    sink = LocalMicSink(noop, source=[], loop=asyncio.get_running_loop())
    await sink.start()

    # meta_analyzer / wake_stream: None, 可讀可寫
    assert sink.meta_analyzer is None
    assert sink.wake_stream is None
    sentinel = object()
    sink.meta_analyzer = sentinel
    assert sink.meta_analyzer is sentinel
    sink.wake_stream = sentinel
    assert sink.wake_stream is sentinel

    # per-user dicts 初始為空
    for attr in (
        "user_buffers",
        "user_is_speaking",
        "user_last_spoken_time",
        "user_first_audio_time",
        "user_last_packet_time",
        "user_near_silence_count",
        "user_wake_check_done",
        "user_wake_check_count",
        "user_utt_max_gap",
    ):
        val = getattr(sink, attr)
        assert isinstance(val, dict), f"{attr} 應為 dict"

    # last_audio_packet_time == 0.0
    assert sink.last_audio_packet_time == 0.0

    # suppress_wake_callback: callable, 預設回 False
    assert callable(sink.suppress_wake_callback)
    assert sink.suppress_wake_callback() is False


@pytest.mark.asyncio
async def test_local_mic_sink_noop_methods_callable():
    """write / elevate_vad / _stream_release 為安全 no-op，呼叫不 raise 且無副作用。"""
    from marvin_voice_core.local_mic_sink import LocalMicSink

    async def noop(user_id, pcm, ts, *, is_wake_check=False):
        pass

    sink = LocalMicSink(noop, source=[], loop=asyncio.get_running_loop())
    await sink.start()

    before_speaking = dict(sink.user_is_speaking)
    before_spoken = dict(sink.user_last_spoken_time)

    sink.write(None, None)
    sink.elevate_vad("local")
    sink._stream_release("local")

    assert sink.user_is_speaking == before_speaking
    assert sink.user_last_spoken_time == before_spoken


@pytest.mark.asyncio
async def test_local_mic_sink_leaves_vad_state_empty():
    """刻意不填 user_is_speaking/user_last_spoken_time：開放麥克風底噪會讓
    _wait_for_user_silence 永遠判「還在講」而擋住 play_tts，故留空讓 silence gate 放行。
    屬性仍存在（介面完整性），但餵語音後保持空 dict。"""
    from marvin_voice_core.local_mic_sink import LocalMicSink

    async def noop(user_id, pcm, ts, *, is_wake_check=False):
        pass

    source = [_speech_frame()] * 6 + [_silence_frame()] * 25
    sink = LocalMicSink(noop, source=source, loop=asyncio.get_running_loop())
    await sink.start()
    await asyncio.sleep(0)

    # 屬性存在（get_active_sink / _wait_for_user_silence 會摸），但保持空
    assert sink.user_is_speaking == {}
    assert sink.user_last_spoken_time == {}
