"""Tests for LocalMicSink.

TDD: 這些測試先於實作撰寫（先紅），實作完成後全綠。
"""
import asyncio
from unittest.mock import MagicMock

import numpy as np
import pytest

from protocols import AudioSource

# 20 ms of 48 kHz stereo int16 PCM: 960 frames × 2 ch × 2 bytes = 3840 bytes
FRAME_BYTES = 3840
# 每幀 20ms → 切句門檻 1.5s = 75 幀（見 silence_cut_s）。給足 80 幀確保觸發。
SILENCE_FRAMES_TO_CUT = 80
# int16 amplitude well above any RMS detection threshold (≈ 8000 RMS)
SPEECH_VALUE = 8000


def _speech_frame() -> bytes:
    return (np.ones(FRAME_BYTES // 2, dtype=np.int16) * SPEECH_VALUE).tobytes()


def _silence_frame() -> bytes:
    return bytes(FRAME_BYTES)


def _ambient_frame(value: int) -> bytes:
    """定值 int16 幀，RMS == |value|（供自適應底噪測試）。"""
    return (np.ones(FRAME_BYTES // 2, dtype=np.int16) * value).tobytes()


@pytest.mark.asyncio
async def test_local_mic_audio_reaches_pipeline_callback():
    """注入語音（6 frames = 23040 bytes > 19200）後接靜音，callback 恰觸發一次且 PCM 非空。"""
    from marvin_voice_core.local_mic_sink import LocalMicSink

    calls: list = []

    async def spy(user_id, pcm, timestamp, *, is_wake_check=False):
        calls.append((user_id, pcm, timestamp))

    # 6 speech frames = 23040 bytes > 19200 min gate, then 靜默達 1.5s 觸發切句
    source = [_speech_frame()] * 6 + [_silence_frame()] * SILENCE_FRAMES_TO_CUT
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
    source = [_speech_frame()] * 4 + [_silence_frame()] * SILENCE_FRAMES_TO_CUT
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
# 時間基準切句 + 自適應底噪（VAD 別切句 / 底噪取樣）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_silence_cut_is_time_based_at_1_5s():
    """切句是時間基準：74 幀(1.48s)不切、76 幀(1.52s)切——證明門檻≈1.5s 而非幀數。"""
    from marvin_voice_core.local_mic_sink import LocalMicSink

    async def run(n_silence):
        calls = []
        async def spy(uid, pcm, ts, *, is_wake_check=False):
            calls.append(pcm)
        source = [_speech_frame()] * 6 + [_silence_frame()] * n_silence
        sink = LocalMicSink(spy, source=source, loop=asyncio.get_running_loop())
        await sink.start()
        await asyncio.sleep(0)
        return len(calls)

    assert await run(74) == 0   # 1.48s < 1.5s → 不切（且無句尾 flush）
    assert await run(76) == 1   # 1.52s ≥ 1.5s → 切


@pytest.mark.asyncio
async def test_intra_sentence_pause_below_threshold_keeps_one_segment():
    """句內 1.0s 停頓（< 1.5s）不切句：兩段語音併成一段，只觸發一次 callback。
    這是「VAD 別切句」的核心行為——自然停頓不再把整句切碎。"""
    from marvin_voice_core.local_mic_sink import LocalMicSink

    calls = []
    async def spy(uid, pcm, ts, *, is_wake_check=False):
        calls.append(pcm)

    # 6 語音 → 1.0s 停頓(50幀) → 6 語音 → 1.6s 靜默(80幀)收尾
    source = ([_speech_frame()] * 6 + [_silence_frame()] * 50
              + [_speech_frame()] * 6 + [_silence_frame()] * SILENCE_FRAMES_TO_CUT)
    sink = LocalMicSink(spy, source=source, loop=asyncio.get_running_loop())
    await sink.start()
    await asyncio.sleep(0)

    assert len(calls) == 1, "句內短停頓不該切成兩段"
    # 兩段語音都在同一 buffer（12 語音幀 = 46080 bytes）
    assert len(calls[0]) == 12 * FRAME_BYTES


@pytest.mark.asyncio
async def test_silence_cut_s_is_configurable():
    """silence_cut_s 可調：設 0.5s → 26 幀(0.52s)即切。"""
    from marvin_voice_core.local_mic_sink import LocalMicSink

    calls = []
    async def spy(uid, pcm, ts, *, is_wake_check=False):
        calls.append(pcm)

    source = [_speech_frame()] * 6 + [_silence_frame()] * 26  # 0.52s
    sink = LocalMicSink(spy, source=source, silence_cut_s=0.5,
                        loop=asyncio.get_running_loop())
    await sink.start()
    await asyncio.sleep(0)

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_sink_samples_noise_floor_not_fixed_threshold():
    """底噪取樣：餵 75 幀穩定 600-RMS 背景後，sink 的 noise_floor 抬到 600
    （非寫死門檻）——動態閾值隨環境上升，吵雜房間才不會把背景當人聲。"""
    from marvin_voice_core.local_mic_sink import LocalMicSink

    async def noop(uid, pcm, ts, *, is_wake_check=False):
        pass

    source = [_ambient_frame(600)] * 75
    sink = LocalMicSink(noop, source=source, loop=asyncio.get_running_loop())
    await sink.start()

    assert sink._noise_floor.noise_floor == 600
    # 沒有寫死的 _rms_threshold 欄位（已被自適應地板取代）
    assert not hasattr(sink, "_rms_threshold")


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

    source = [_speech_frame()] * 6 + [_silence_frame()] * SILENCE_FRAMES_TO_CUT
    sink = LocalMicSink(noop, source=source, loop=asyncio.get_running_loop())
    await sink.start()
    await asyncio.sleep(0)

    # 屬性存在（get_active_sink / _wait_for_user_silence 會摸），但保持空
    assert sink.user_is_speaking == {}
    assert sink.user_last_spoken_time == {}


# ---------------------------------------------------------------------------
# on_speech_start_callback — 起音邊緣 (silence→speech onset)
# ---------------------------------------------------------------------------


def test_on_speech_start_param_defaults_to_noop_callable():
    """on_speech_start_callback 預設為可呼叫的 no-op：不傳時不 raise，且 callable。"""
    from marvin_voice_core.local_mic_sink import LocalMicSink

    async def noop_cut(uid, pcm, ts, *, is_wake_check=False):
        pass

    sink = LocalMicSink(noop_cut, source=[])
    assert callable(sink.on_speech_start_callback)
    sink.on_speech_start_callback("local")  # 不 raise


@pytest.mark.asyncio
async def test_onset_fires_once_on_speech_start():
    """on_speech_start 在靜默→人聲邊緣恰觸發一次（MagicMock 同步 callback）。"""
    from marvin_voice_core.local_mic_sink import LocalMicSink

    onset_cb = MagicMock(name="onset")

    async def cut_spy(uid, pcm, ts, *, is_wake_check=False):
        pass

    source = [_speech_frame()] * 6 + [_silence_frame()] * SILENCE_FRAMES_TO_CUT
    sink = LocalMicSink(cut_spy, on_speech_start_callback=onset_cb,
                        source=source, loop=asyncio.get_running_loop())
    await sink.start()
    await asyncio.sleep(0)

    assert onset_cb.call_count == 1
    onset_cb.assert_called_once_with(sink._user_id)


@pytest.mark.asyncio
async def test_onset_does_not_fire_on_pure_silence():
    """全靜音時 on_speech_start 不觸發。"""
    from marvin_voice_core.local_mic_sink import LocalMicSink

    onset_cb = MagicMock(name="onset")

    async def cut_spy(uid, pcm, ts, *, is_wake_check=False):
        pass

    source = [_silence_frame()] * 30
    sink = LocalMicSink(cut_spy, on_speech_start_callback=onset_cb,
                        source=source, loop=asyncio.get_running_loop())
    await sink.start()
    await asyncio.sleep(0)

    assert onset_cb.call_count == 0


@pytest.mark.asyncio
async def test_onset_refires_on_new_utterance_after_cut():
    """切句後 _is_speaking 重設為 False，下一句再次觸發 on_speech_start（re-arm）。"""
    from marvin_voice_core.local_mic_sink import LocalMicSink

    onset_cb = MagicMock(name="onset")

    async def cut_spy(uid, pcm, ts, *, is_wake_check=False):
        pass

    # 兩段完整語音，各自觸發切句後 re-arm
    source = (
        [_speech_frame()] * 6 + [_silence_frame()] * SILENCE_FRAMES_TO_CUT
        + [_speech_frame()] * 6 + [_silence_frame()] * SILENCE_FRAMES_TO_CUT
    )
    sink = LocalMicSink(cut_spy, on_speech_start_callback=onset_cb,
                        source=source, loop=asyncio.get_running_loop())
    await sink.start()
    await asyncio.sleep(0)

    assert onset_cb.call_count == 2


@pytest.mark.asyncio
async def test_onset_fires_before_cut():
    """on_speech_start 同步觸發（start() 內迴圈中），嚴格先於非同步 create_task 切句。"""
    from marvin_voice_core.local_mic_sink import LocalMicSink

    order: list = []
    onset_cb = MagicMock(name="onset", side_effect=lambda uid: order.append("onset"))

    cut_calls: list = []

    async def cut_spy(uid, pcm, ts, *, is_wake_check=False):
        cut_calls.append(pcm)
        order.append("cut")

    source = [_speech_frame()] * 6 + [_silence_frame()] * SILENCE_FRAMES_TO_CUT
    sink = LocalMicSink(cut_spy, on_speech_start_callback=onset_cb,
                        source=source, loop=asyncio.get_running_loop())
    await sink.start()
    # onset 已在 start() 內同步觸發；cut 尚在 task 佇列未執行
    assert onset_cb.call_count == 1
    assert len(cut_calls) == 0

    await asyncio.sleep(0)  # 讓 create_task 執行
    assert len(cut_calls) == 1
    assert order == ["onset", "cut"]
