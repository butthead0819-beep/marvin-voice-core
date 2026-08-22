"""Plan 12 LocalMixingAudioSource — always-on f32 混音 source。

關鍵不變量（讀在 discord voice thread 上、驅動全部音訊）：
  ⚠ idle 時 read() 回 silence frame（3840 bytes 全零），絕不回 None/b""（否則 discord 停播＝全死）
  ⚠ read() 內部任何例外 → 回 silence、永不 raise（single point of failure）
其餘：music-only / music+TTS overlay+duck / TTS 佇列消化 / is_idle / tts_load_seconds /
buffer cap / lock-free 並發 push / 音量即時。
"""
from __future__ import annotations

import threading

import numpy as np
import pytest

from unittest.mock import MagicMock

import time as _time

from local_mixing_source import (
    LocalMixingAudioSource,
    MixerPlaybackAdapter,
    S16ToF32MusicSource,
    BufferedF32MusicSource,
    PreloadedF32MusicSource,
    preload_f32_source,
    ensure_mixer_playing,
    FRAME_SAMPLES,
    FRAME_BYTES_S16,
    FRAME_BYTES_F32,
    SAMPLE_RATE,
    CHANNELS,
)

import audio_mixing as am

from marvin_voice_core.playback_device import DiscordPlaybackDevice


def _f32_frame(value=0.5, n=FRAME_SAMPLES):
    return np.full(n, value, dtype=np.float32)


class _FakeMusic:
    """music layer fake：每次 read() 回 f32le bytes，count 次後耗盡回 b""。"""

    def __init__(self, value=0.4, frames=3):
        self._value = value
        self._left = frames

    def read(self):
        if self._left <= 0:
            return b""
        self._left -= 1
        return np.full(FRAME_SAMPLES, self._value, dtype=np.float32).tobytes()


class _BoomMusic:
    def read(self):
        raise RuntimeError("ffmpeg boom")


# ── 不變量 ⚠ ──────────────────────────────────────────────────────────────────

def test_read_idle_returns_silence_frame_never_none():
    mix = LocalMixingAudioSource()
    out = mix.read()
    assert isinstance(out, bytes)
    assert len(out) == FRAME_BYTES_S16
    assert out == b"\x00" * FRAME_BYTES_S16  # 全零 silence


def test_read_never_raises_on_internal_error_returns_silence():
    mix = LocalMixingAudioSource()
    mix.set_music_source(_BoomMusic())
    out = mix.read()  # 不可 raise
    assert isinstance(out, bytes)
    assert len(out) == FRAME_BYTES_S16


def test_read_always_returns_full_frame_length():
    mix = LocalMixingAudioSource(seed=1)
    mix.set_music_source(_FakeMusic(value=0.3, frames=2))
    for _ in range(4):
        assert len(mix.read()) == FRAME_BYTES_S16


def test_is_opus_false():
    assert LocalMixingAudioSource().is_opus() is False


# ── music-only ───────────────────────────────────────────────────────────────

def test_read_music_only_matches_dsp_pipeline():
    mix = LocalMixingAudioSource(seed=7, volume=0.5)
    mix.set_music_source(_FakeMusic(value=0.4, frames=1))
    out = np.frombuffer(mix.read(), dtype=np.int16)
    # 期望：music(0.4) * volume(0.5) * duck(ramp 起點 1.0) → dither(seed7) → s16
    music = _f32_frame(0.4)
    expected = am.to_s16(am.tpdf_dither(am.apply_gain(music, 0.5 * 1.0), np.random.default_rng(7)))
    assert np.array_equal(out, expected)


# ── music + TTS overlay ──────────────────────────────────────────────────────

def test_music_and_tts_both_contribute():
    mix = LocalMixingAudioSource(seed=3, volume=1.0, duck_level=0.5, duck_step=1.0)
    mix.set_music_source(_FakeMusic(value=0.2, frames=5))
    mix.push_tts(_f32_frame(0.3))
    mixed = np.frombuffer(mix.read(), dtype=np.int16).astype(np.int32)
    # 與只有 music / 只有 tts 的輸出都不同 → 兩層都進了
    assert mixed.mean() != 0


# ── TTS 音量（tts_gain）─────────────────────────────────────────────────────

def test_tts_gain_default_is_1_0():
    """2026-08-22 用戶要求：satellite 模式下音樂 1.0、TTS 也是 1.0，tts_gain 預設 1.0。"""
    assert LocalMixingAudioSource()._tts_gain == 1.0


def test_tts_layer_scaled_by_tts_gain():
    """TTS（Marvin）層套 tts_gain：輸出 == apply_gain(tts, tts_gain) 走完 DSP。"""
    mix = LocalMixingAudioSource(seed=11, tts_gain=0.5)
    mix.push_tts(_f32_frame(0.6, n=FRAME_SAMPLES))
    out = np.frombuffer(mix.read(), dtype=np.int16)
    tts = _f32_frame(0.6)
    expected = am.to_s16(am.tpdf_dither(am.apply_gain(tts, 0.5), np.random.default_rng(11)))
    assert np.array_equal(out, expected)


def test_marmo_interject_layer_also_scaled_by_tts_gain():
    """打岔層 Marmo 同為 TTS → 也套 tts_gain，避免比 Marvin 大聲。"""
    mix = LocalMixingAudioSource(seed=5, tts_gain=0.5)
    mix.push_tts2(_f32_frame(0.4, n=FRAME_SAMPLES))
    out = np.frombuffer(mix.read(), dtype=np.int16)
    tts2 = _f32_frame(0.4)
    expected = am.to_s16(am.tpdf_dither(am.apply_gain(tts2, 0.5), np.random.default_rng(5)))
    assert np.array_equal(out, expected)


def test_tts_gain_unity_keeps_full_volume():
    """tts_gain=1.0 → 退回滿音量（不破壞既有滿音量語意）。"""
    mix = LocalMixingAudioSource(seed=2, tts_gain=1.0)
    mix.push_tts(_f32_frame(0.5, n=FRAME_SAMPLES))
    out = np.frombuffer(mix.read(), dtype=np.int16)
    tts = _f32_frame(0.5)
    expected = am.to_s16(am.tpdf_dither(am.apply_gain(tts, 1.0), np.random.default_rng(2)))
    assert np.array_equal(out, expected)


# ── TTS 佇列消化 ─────────────────────────────────────────────────────────────

def test_tts_queue_consumed_in_order_then_idle():
    mix = LocalMixingAudioSource(seed=1)
    mix.push_tts(_f32_frame(0.5, n=FRAME_SAMPLES))      # 剛好一幀
    mix.push_tts(_f32_frame(0.25, n=FRAME_SAMPLES))     # 第二幀
    assert not mix.is_idle()
    mix.read()  # 消化 buffer1
    mix.read()  # 消化 buffer2
    assert mix.is_idle()  # 佇列空


def test_tts_subframe_clip_consumed_in_one_read():
    mix = LocalMixingAudioSource(seed=1)
    mix.push_tts(_f32_frame(0.5, n=FRAME_SAMPLES // 2))  # 半幀
    assert not mix.is_idle()
    mix.read()
    assert mix.is_idle()


# ── player-duck barge-in 閘：只在 Marvin 正播 TTS 時才 arm ────────────────────

def test_note_player_speech_no_arm_when_marvin_silent():
    """Marvin 沒在講 → 玩家說話不是打斷，不 arm player-duck（否則緊接的點歌 ack 被壓）。"""
    mix = LocalMixingAudioSource(clock=lambda: 100.0)
    mix.note_player_speech()
    assert mix._player_speech_until == 0.0   # 未 arm


def test_note_player_speech_arms_when_marvin_speaking():
    """Marvin 正播長播報（佇列有料）+ 玩家插話 = barge-in → arm，讓 Marvin 讓路。
    單次觸發只是 onset（可能只是雜音）→ 短暫淺 duck，不是直接整段 5s 深 duck。"""
    mix = LocalMixingAudioSource(clock=lambda: 100.0)
    mix.push_tts(_f32_frame(0.5, n=FRAME_SAMPLES))   # Marvin 正在講
    mix.note_player_speech()
    assert mix._player_speech_until == 101.0          # onset armed（+onset_hold 1s，非 5s）
    assert mix._player_speech_confirmed is False


def test_note_player_speech_confirms_on_second_call_within_window():
    """onset 窗內再次觸發 → 判定是真的在講話，升級到 confirmed 並延長到完整 5s hold。"""
    clk = [100.0]
    mix = LocalMixingAudioSource(clock=lambda: clk[0])
    mix.push_tts(_f32_frame(0.5, n=FRAME_SAMPLES))
    mix.note_player_speech()          # onset：until=101.0
    clk[0] = 100.5                    # 仍在 confirm window（1.2s）內
    mix.note_player_speech()          # 確認 → confirmed
    assert mix._player_speech_confirmed is True
    assert mix._player_speech_until == 105.5          # +hold 5s


def test_note_player_speech_onset_not_confirmed_after_window_expires():
    """onset 窗過了才又觸發 → 視為全新一輪 onset，不會誤判成確認。"""
    clk = [100.0]
    mix = LocalMixingAudioSource(clock=lambda: clk[0])
    mix.push_tts(_f32_frame(0.5, n=FRAME_SAMPLES))
    mix.note_player_speech()          # onset：confirm_deadline=101.2
    clk[0] = 102.0                    # confirm window 已過
    mix.note_player_speech()          # 新一輪 onset，非確認
    assert mix._player_speech_confirmed is False
    assert mix._player_speech_until == 103.0          # 102.0 + onset_hold 1s


def test_ack_after_request_plays_full_volume_not_ducked():
    """回歸：串流中語音點歌 → STT(note_player_speech) → 緊接 ack。Marvin 當下沒在講，
    ack 是全新 onset，該全 tts_gain 播出，不該被 player-duck 壓（前小後大 bug）。"""
    mix = LocalMixingAudioSource(seed=1, tts_gain=0.5, clock=lambda: 100.0)
    mix.note_player_speech()                          # 玩家點歌那次 STT
    mix.push_tts(_f32_frame(0.6, n=FRAME_SAMPLES))    # 回應 ack
    out = np.frombuffer(mix.read(), dtype=np.int16)
    expected = am.to_s16(am.tpdf_dither(
        am.apply_gain(_f32_frame(0.6), 0.5), np.random.default_rng(1)))
    assert np.array_equal(out, expected)              # 全 tts_gain，未被壓


# ── is_idle / 狀態欄位 ────────────────────────────────────────────────────────

def test_is_idle_transitions():
    mix = LocalMixingAudioSource()
    assert mix.is_idle() is True
    assert mix.is_playing_audio is False
    src = _FakeMusic(frames=2)
    mix.set_music_source(src)
    assert mix.is_idle() is False
    assert mix.is_playing_audio is True
    mix.clear_music()
    assert mix.is_idle() is True


def test_tts_load_seconds_reflects_queued_samples():
    mix = LocalMixingAudioSource()
    # 一整秒 = SAMPLE_RATE * CHANNELS interleaved samples
    one_sec = np.zeros(SAMPLE_RATE * CHANNELS, dtype=np.float32)
    mix.push_tts(one_sec)
    assert mix.tts_load_seconds() == pytest.approx(1.0, abs=0.01)
    assert mix.tts_queue_duration == pytest.approx(1.0, abs=0.01)


# ── buffer cap (OV #6) ───────────────────────────────────────────────────────

def test_push_tts_rejects_when_over_cap():
    mix = LocalMixingAudioSource(tts_cap_seconds=1.0)
    half = np.zeros(SAMPLE_RATE * CHANNELS // 2, dtype=np.float32)  # 0.5s
    assert mix.push_tts(half) is True   # 0.5s ok
    assert mix.push_tts(half) is True   # 1.0s ok (剛好到上限)
    assert mix.push_tts(half) is False  # 超過 → 拒絕、不入隊


# ── ducking ramp ─────────────────────────────────────────────────────────────

def test_ducking_ramps_music_down_when_tts_active():
    # duck_step 小 → 可觀察逐幀下降；music 持續、TTS 持續
    mix = LocalMixingAudioSource(seed=1, volume=1.0, duck_level=0.2, duck_step=0.1)
    mix.set_music_source(_FakeMusic(value=0.5, frames=100))
    for _ in range(10):
        mix.push_tts(_f32_frame(0.0))  # 靜音 TTS 但「存在」→ 觸發 duck
    g0 = mix._duck_cur
    mix.read()
    g1 = mix._duck_cur
    assert g1 < g0  # 往 duck_level 下降


def test_ducking_restores_when_tts_gone():
    mix = LocalMixingAudioSource(seed=1, volume=1.0, duck_level=0.2, duck_step=0.5)
    mix.set_music_source(_FakeMusic(value=0.5, frames=100))
    mix.push_tts(_f32_frame(0.0))
    mix.read()              # tts 消化 + duck 下降
    low = mix._duck_cur
    mix.read()              # tts 沒了 → 回升
    mix.read()
    assert mix._duck_cur > low


def test_default_music_duck_step_is_smooth_not_abrupt():
    """用戶回饋：預設 duck_step 太大（3 幀=60ms 就到底），聽感是瞬降的「悶」一聲。
    要求至少要 10 幀（200ms）才 ramp 到 duck_level，聽起來才是漸弱而非瞬降。"""
    mix = LocalMixingAudioSource(seed=1, volume=1.0)
    mix.set_music_source(_FakeMusic(value=0.5, frames=100))
    mix.push_tts(_f32_frame(0.0, n=FRAME_SAMPLES * 20))
    frames_to_settle = 0
    for _ in range(30):
        mix.read()
        frames_to_settle += 1
        if abs(mix._duck_cur - mix._duck_level) < 1e-6:
            break
    assert frames_to_settle >= 10, f"duck 只花 {frames_to_settle} 幀就到底，太突兀"


def test_default_tts_player_duck_step_is_smooth_but_steeper_than_before():
    """玩家確認持續說話（confirmed）→ TTS 讓路到 10% 的 ramp 仍要逐幀漸進、不瞬跳；
    但用戶回饋原本的 gradient 太軟，新預設 step 要比舊版（0.06）更陡。"""
    clk = [0.0]
    mix = LocalMixingAudioSource(clock=lambda: clk[0])
    mix.push_tts(_f32_frame(0.5, n=FRAME_SAMPLES))
    mix.note_player_speech()
    mix.note_player_speech()  # 仍在 confirm window 內再觸發一次 → confirmed（目標 10%）
    frames_to_settle = 0
    for _ in range(30):
        mix._tts_player_duck_step_toward(0.5)
        frames_to_settle += 1
        if abs(mix._tts_player_duck_cur - mix._tts_player_duck_level) < 1e-6:
            break
    assert 2 <= frames_to_settle <= 8, (
        f"TTS player duck 花 {frames_to_settle} 幀到底，應逐幀漸進但比舊版更陡"
    )


# ── lock-free 並發 push ───────────────────────────────────────────────────────

def test_concurrent_push_during_read_no_corruption():
    mix = LocalMixingAudioSource(seed=1)
    mix.set_music_source(_FakeMusic(value=0.1, frames=10_000))
    errors = []

    def producer():
        try:
            for _ in range(200):
                mix.push_tts(_f32_frame(0.2, n=FRAME_SAMPLES))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=producer)
    t.start()
    for _ in range(200):
        assert len(mix.read()) == FRAME_BYTES_S16
    t.join()
    assert errors == []


# ── MixerPlaybackAdapter（reconnect-safe，OV #4）──────────────────────────────

def test_adapter_delegates_read_and_opus():
    mix = LocalMixingAudioSource()
    adapter = MixerPlaybackAdapter(mix)
    assert adapter.is_opus() is False
    assert adapter.read() == b"\x00" * FRAME_BYTES_S16  # 委派到 mixer idle silence


def test_adapter_cleanup_preserves_mixer_state():
    mix = LocalMixingAudioSource()
    mix.push_tts(_f32_frame(0.3))
    adapter = MixerPlaybackAdapter(mix)
    adapter.cleanup()  # discord 停播會呼叫；不可清掉持久 mixer 狀態
    assert mix.is_idle() is False  # TTS 還在


# ── S16ToF32MusicSource（重用 FFmpegPCMAudio s16，轉 f32 給音樂層）────────────

class _FakeS16:
    def __init__(self, samples, chunks=1):
        self._buf = np.asarray(samples, dtype=np.int16).tobytes()
        self._chunks = chunks

    def read(self):
        if self._chunks <= 0:
            return b""
        self._chunks -= 1
        return self._buf


def test_s16_to_f32_converts_scale():
    src = S16ToF32MusicSource(_FakeS16([32767, -32768, 0, 16384], chunks=1))
    out = np.frombuffer(src.read(), dtype=np.float32)
    assert np.allclose(out, [32767 / 32768, -1.0, 0.0, 0.5], atol=1e-6)


def test_s16_to_f32_exhausts_returns_empty():
    src = S16ToF32MusicSource(_FakeS16([1, 2], chunks=1))
    assert src.read() != b""
    assert src.read() == b""  # 來源耗盡 → b""（mixer 據此清音樂層）


def test_s16_to_f32_feeds_mixer_music_layer():
    mix = LocalMixingAudioSource(seed=1, volume=1.0)
    mix.set_music_source(S16ToF32MusicSource(_FakeS16([8192] * FRAME_SAMPLES, chunks=2)))
    assert len(mix.read()) == FRAME_BYTES_S16
    assert not mix.is_idle()


def test_has_music_tracks_source_drain():
    mix = LocalMixingAudioSource(seed=1)
    assert mix.has_music() is False
    mix.set_music_source(_FakeMusic(value=0.2, frames=1))
    assert mix.has_music() is True
    mix.read()   # 消化唯一一幀
    mix.read()   # 來源回 b"" → mixer 清音樂層
    assert mix.has_music() is False


# ── PreloadedF32MusicSource / preload_f32_source（mixer decode underrun 真解：
#    整首先解碼進記憶體，read() 純陣列取用，消除 ffmpeg pipe 即時解碼被 CPU 搶佔導致
#    的中段爆音，見 project_car_puck_pops_and_1s_dropout_2026-07-25）───────────────

def test_preload_reads_all_frames_in_order_then_eof():
    inner = _FakeF32Frames([0.1, 0.2, 0.3])
    src = preload_f32_source(inner)
    got = []
    while True:
        b = src.read()
        if b == b"":
            break
        got.append(round(float(np.frombuffer(b, dtype=np.float32)[0]), 4))
    assert got == [0.1, 0.2, 0.3]


def test_preload_calls_inner_cleanup_during_preload_not_on_source_cleanup():
    inner = _FakeF32Frames([0.1])
    src = preload_f32_source(inner)
    assert inner.cleaned is True   # 解碼完當下就清（ffmpeg 已無存在必要）
    src.cleanup()                  # 再呼叫一次不可 raise（no-op）


def test_preload_handles_empty_source():
    class _Empty:
        def read(self):
            return b""

        def cleanup(self):
            pass

    src = preload_f32_source(_Empty())
    assert src.read() == b""


def test_preload_never_returns_silence_underrun_frame():
    """跟 BufferedF32MusicSource 不同：沒有 underrun 概念，耗盡就是 b""，不會塞靜音頂替。"""
    inner = _FakeF32Frames([0.5])
    src = preload_f32_source(inner)
    src.read()
    assert src.read() == b""


def test_preload_feeds_mixer_music_layer():
    mix = LocalMixingAudioSource(seed=1, volume=1.0)
    inner = _FakeF32Frames([0.2, 0.2, 0.2])
    mix.set_music_source(preload_f32_source(inner))
    assert len(mix.read()) == FRAME_BYTES_S16
    assert mix.has_music()


def test_preloaded_source_is_reusable_container_type():
    src = preload_f32_source(_FakeF32Frames([0.1, 0.2]))
    assert isinstance(src, PreloadedF32MusicSource)


def test_preloaded_stats_matches_buffered_shape_no_underruns():
    """_mixer_play_music 退出時呼叫 .stats() 印 log，preload 沒有 underrun 概念但欄位形狀
    要跟 BufferedF32MusicSource.stats() 一致，才不會炸 log 那一行。"""
    src = preload_f32_source(_FakeF32Frames([0.1, 0.2, 0.3]))
    st = src.stats()
    assert st["underruns"] == 0
    assert st["produced"] == 3
    assert st["eof"] is True
    assert st["eof_reason"] == "preloaded"


# ── ensure_mixer_playing ─────────────────────────────────────────────────────

def _vc(connected=True, playing=False):
    vc = MagicMock()
    vc.is_connected.return_value = connected
    vc.is_playing.return_value = playing
    return vc


def test_ensure_playing_plays_when_idle_vc():
    mix = LocalMixingAudioSource()
    vc = _vc(connected=True, playing=False)
    device = DiscordPlaybackDevice(vc)
    assert ensure_mixer_playing(device, lambda: MixerPlaybackAdapter(mix)) is True
    assert vc.play.call_count == 1
    assert isinstance(vc.play.call_args.args[0], MixerPlaybackAdapter)


def test_ensure_playing_idempotent_when_already_playing():
    mix = LocalMixingAudioSource()
    vc = _vc(connected=True, playing=True)
    device = DiscordPlaybackDevice(vc)
    assert ensure_mixer_playing(device, lambda: MixerPlaybackAdapter(mix)) is False
    assert not vc.play.called


def test_ensure_playing_no_vc():
    mix = LocalMixingAudioSource()
    assert ensure_mixer_playing(None, lambda: MixerPlaybackAdapter(mix)) is False


def test_ensure_playing_not_connected():
    mix = LocalMixingAudioSource()
    vc = _vc(connected=False, playing=False)
    device = DiscordPlaybackDevice(vc)
    assert ensure_mixer_playing(device, lambda: MixerPlaybackAdapter(mix)) is False
    assert not vc.play.called


def test_ensure_playing_swallows_already_playing_race():
    mix = LocalMixingAudioSource()
    vc = _vc(connected=True, playing=False)
    vc.play.side_effect = RuntimeError("Already playing audio")  # TOCTOU race
    device = DiscordPlaybackDevice(vc)
    assert ensure_mixer_playing(device, lambda: MixerPlaybackAdapter(mix)) is False  # 不 raise


def test_ensure_playing_fresh_adapter_each_call():
    mix = LocalMixingAudioSource()
    seen = []
    factory = lambda: MixerPlaybackAdapter(mix)  # noqa: E731
    ensure_mixer_playing(DiscordPlaybackDevice(_vc(playing=False)), lambda: seen.append(factory()) or seen[-1])
    ensure_mixer_playing(DiscordPlaybackDevice(_vc(playing=False)), lambda: seen.append(factory()) or seen[-1])
    assert len(seen) == 2 and seen[0] is not seen[1]  # 每次新 adapter，不重用


# ── BufferedF32MusicSource（bug 1 修：背景預讀解耦 ffmpeg pipe）────────────────

class _FakeF32Frames:
    """回傳一串 f32 frame（每幀值不同好辨識），耗盡回 b""。"""

    def __init__(self, values):
        self._frames = [np.full(FRAME_SAMPLES, v, dtype=np.float32).tobytes() for v in values]
        self._i = 0
        self.cleaned = False

    def read(self):
        if self._i >= len(self._frames):
            return b""
        f = self._frames[self._i]
        self._i += 1
        return f

    def cleanup(self):
        self.cleaned = True


def test_buffered_passes_all_frames_in_order_then_eof():
    inner = _FakeF32Frames([0.1, 0.2, 0.3])
    buf = BufferedF32MusicSource(inner, buffer_frames=10)
    got = []
    for _ in range(300):
        b = buf.read()
        if b == b"":
            break
        f = np.frombuffer(b, dtype=np.float32)
        if f.any():  # 跳過 underrun silence
            got.append(round(float(f[0]), 4))
        _time.sleep(0.001)
    buf.cleanup()
    assert got == [0.1, 0.2, 0.3]  # 順序 + 內容 + 自然 eof


def test_buffered_underrun_returns_silence_not_eof():
    gate = threading.Event()

    class _Gated:
        def read(self):
            gate.wait(1.0)
            return np.full(FRAME_SAMPLES, 0.5, dtype=np.float32).tobytes()

        def cleanup(self):
            pass

    buf = BufferedF32MusicSource(_Gated(), buffer_frames=4)
    _time.sleep(0.05)  # bg thread 卡在 inner.read() → buffer 空、未 eof
    out = buf.read()
    assert out == b"\x00" * FRAME_BYTES_F32  # underrun → silence，不是 b""（不可停歌）
    gate.set()  # 放行，讓 bg thread 能產幀後正常退出
    buf.cleanup()


def test_buffered_stats_reports_produced_and_eof_reason():
    """診斷：正常耗盡 → produced 計到全部幀、eof=True、eof_reason='empty'。"""
    inner = _FakeF32Frames([0.1, 0.2, 0.3])
    buf = BufferedF32MusicSource(inner, buffer_frames=10)
    for _ in range(300):
        if buf.read() == b"":
            break
        _time.sleep(0.001)
    st = buf.stats()
    buf.cleanup()
    assert st["produced"] == 3
    assert st["eof"] is True
    assert st["eof_reason"] == "empty"


def test_buffered_stats_eof_reason_error_on_inner_exception():
    """診斷：inner.read 拋例外 → eof_reason='error'（區分音源死於錯誤 vs 正常耗盡）。"""
    class _Boom:
        def read(self):
            raise RuntimeError("stream died")

        def cleanup(self):
            pass

    buf = BufferedF32MusicSource(_Boom(), buffer_frames=4)
    _time.sleep(0.05)
    st = buf.stats()
    buf.cleanup()
    assert st["eof"] is True
    assert st["eof_reason"] == "error"
    assert st["produced"] == 0


def test_buffered_cleanup_stops_thread_and_inner():
    inner = _FakeF32Frames([0.1])
    buf = BufferedF32MusicSource(inner, buffer_frames=4)
    _time.sleep(0.03)
    buf.cleanup()
    assert inner.cleaned is True
    assert not buf._thread.is_alive()


def test_buffered_feeds_mixer_music_layer():
    mix = LocalMixingAudioSource(seed=1, volume=1.0)
    inner = _FakeF32Frames([0.2, 0.2, 0.2])
    mix.set_music_source(BufferedF32MusicSource(inner, buffer_frames=10))
    _time.sleep(0.03)
    assert len(mix.read()) == FRAME_BYTES_S16
    assert mix.has_music()
    mix.clear_music()
    assert inner.cleaned is True  # clear_music 連帶 cleanup buffered 來源


def test_set_music_source_cleans_previous():
    a = _FakeF32Frames([0.1])
    b = _FakeF32Frames([0.2])
    mix = LocalMixingAudioSource(seed=1)
    sa = BufferedF32MusicSource(a, buffer_frames=4)
    sb = BufferedF32MusicSource(b, buffer_frames=4)
    mix.set_music_source(sa)
    mix.set_music_source(sb)   # 換源 → 舊源被 cleanup
    _time.sleep(0.02)
    assert a.cleaned is True
    mix.clear_music()


# ── Instrumentation（A：下輪 live 收數據判 mixer 是否跟得上）───────────────────

def test_buffered_counts_underruns_and_exposes_stats():
    gate = threading.Event()

    class _Gated:
        def read(self):
            gate.wait(1.0)
            return np.full(FRAME_SAMPLES, 0.5, dtype=np.float32).tobytes()

        def cleanup(self):
            pass

    buf = BufferedF32MusicSource(_Gated(), buffer_frames=4)
    _time.sleep(0.05)  # bg 卡住 → 空 buffer
    buf.read(); buf.read()  # 兩次 underrun
    st = buf.stats()
    assert st["underruns"] >= 2
    assert st["max"] == 4
    assert "depth" in st
    gate.set()
    buf.cleanup()


def test_instrument_mode_read_still_returns_full_frame():
    mix = LocalMixingAudioSource(seed=1, instrument=True)
    mix.set_music_source(_FakeMusic(value=0.3, frames=3))
    for _ in range(5):
        assert len(mix.read()) == FRAME_BYTES_S16  # instrument 不破壞 read()


def test_instrument_off_by_default():
    assert LocalMixingAudioSource()._instrument is False


# ── on-demand 模式（修 always-on×DAVE：idle 停送、內容到再 arm）─────────────────

def test_on_demand_idle_returns_silence_then_empty_after_grace():
    # grace 3 幀：前 3 次 idle 回 silence，第 4 次起回 b""（讓 discord 停送）
    mix = LocalMixingAudioSource(seed=1, on_demand=True, idle_grace_s=0.06)  # 0.06/0.02=3
    outs = [mix.read() for _ in range(6)]
    assert outs[0] == b"\x00" * FRAME_BYTES_S16   # idle 但在 grace 內 → silence
    assert outs[2] == b"\x00" * FRAME_BYTES_S16
    assert outs[3] == b""                          # 超過 grace → b""（停送）
    assert outs[5] == b""


def test_on_demand_content_resets_idle_and_plays():
    mix = LocalMixingAudioSource(seed=1, on_demand=True, idle_grace_s=0.06)
    for _ in range(5):
        mix.read()                                 # idle 累積、已回 b""
    mix.set_music_source(_FakeMusic(value=0.3, frames=10))
    out = mix.read()
    assert out != b"" and len(out) == FRAME_BYTES_S16  # 有內容 → 正常幀、idle 重設
    assert mix._idle_count == 0


def test_always_on_default_idle_never_returns_empty():
    # 預設 on_demand=False：idle 永遠 silence、絕不 b""（always-on 不變）
    mix = LocalMixingAudioSource(seed=1)
    for _ in range(100):
        assert mix.read() == b"\x00" * FRAME_BYTES_S16


def test_clear_tts_drops_queued_and_current():
    mix = LocalMixingAudioSource(seed=1)
    mix.push_tts(_f32_frame(0.5, n=FRAME_SAMPLES))
    mix.push_tts(_f32_frame(0.3, n=FRAME_SAMPLES))
    mix.read()  # 取一幀（_tts_cur 設起來）
    assert not mix.is_idle()
    mix.clear_tts()
    assert mix.is_idle()                 # 佇列 + 當前都清掉
    assert mix.tts_load_seconds() == 0.0


# ── 打岔層 layer2（Marmo 疊進來打斷 Marvin）─────────────────────────────────────

def test_tts2_layer_ramps_layer1_duck_gradually():
    """layer2(Marmo) 進來時 layer1(Marvin) 逐漸 fade out（非瞬降）→ 用戶要的漸進 ducking。"""
    mix = LocalMixingAudioSource(seed=3)
    n = FRAME_SAMPLES * 70  # 夠長撐過 ramp
    mix.push_tts(_f32_frame(0.5, n=n))    # Marvin
    mix.push_tts2(_f32_frame(0.3, n=n))   # Marmo 同時進來

    def _level():
        return np.frombuffer(mix.read(), dtype=np.int16).astype(np.float32).mean() / 32767.0

    first = _level()           # 第一幀：layer1 幾乎還沒 duck（剛開始 fade）
    settled = first
    # ramp 1.0→duck 走完（step 0.010、range≈0.4 → ~40 幀；多讀確保穩定）
    for _ in range(60):
        settled = _level()
    assert first > settled     # 漸進：第一幀比穩定後大聲（Marvin 還沒 fade 下去）
    # 兩層皆套 tts_gain；layer1 再乘 interject_duck，layer2 不被 duck。
    assert settled == pytest.approx(mix._tts_gain * (0.5 * mix._interject_duck + 0.3), abs=0.02)


def test_tts2_scaled_by_tts_gain_not_ducked_by_layer1():
    """只有 layer2 在播 → Marmo 套 tts_gain（同為 TTS），但不被 layer1 duck。"""
    mix = LocalMixingAudioSource(seed=3)
    mix.push_tts2(_f32_frame(0.4))
    out = np.frombuffer(mix.read(), dtype=np.int16).astype(np.float32) / 32767.0
    assert out.mean() == pytest.approx(0.4 * mix._tts_gain, abs=0.01)


def test_tts2_makes_mixer_non_idle_and_counts_load():
    mix = LocalMixingAudioSource(seed=1)
    assert mix.is_idle()
    mix.push_tts2(_f32_frame(0.5, n=FRAME_SAMPLES))
    assert not mix.is_idle()
    assert mix.is_playing_audio
    assert mix.tts_load_seconds() > 0
    mix.read()  # 消化
    assert mix.is_idle()


def test_clear_tts_clears_both_layers():
    mix = LocalMixingAudioSource(seed=1)
    mix.push_tts(_f32_frame(0.5))
    mix.push_tts2(_f32_frame(0.3))
    assert not mix.is_idle()
    mix.clear_tts()
    assert mix.is_idle()


def test_push_tts2_rejects_when_over_cap():
    mix = LocalMixingAudioSource(seed=1, tts_cap_seconds=1.0)
    half = _f32_frame(0.5, n=int(SAMPLE_RATE * CHANNELS * 0.5))
    assert mix.push_tts2(half) is True
    assert mix.push_tts2(half) is True   # 剛好到上限
    assert mix.push_tts2(half) is False  # 超過 → 拒絕


# ── TTS 對玩家說話 duck（玩家還在講 → Marvin TTS 讓路到 10%，5s 無聲才回） ──────

def test_tts_player_duck_ramps_down_to_onset_level_on_first_speech():
    """單次 note_player_speech()（可能只是雜音）→ 只淺 duck 到 onset 80%，不直接到 10%。"""
    clk = [0.0]
    mix = LocalMixingAudioSource(clock=lambda: clk[0])
    assert mix._tts_player_duck_cur == 1.0
    mix.push_tts(_f32_frame(0.5, n=FRAME_SAMPLES))  # Marvin 正播長播報＝arm 前提
    mix.note_player_speech()                       # onset only
    for _ in range(50):
        mix._tts_player_duck_step_toward(0.5)      # 仍在 onset 窗內
    assert abs(mix._tts_player_duck_cur - mix._tts_player_duck_level_onset) < 1e-6  # → 80%


def test_tts_player_duck_ramps_down_to_confirmed_level_when_speech_continues():
    """onset 窗內再講一次（確認）→ 才繼續往 confirmed 10% 降。"""
    clk = [0.0]
    mix = LocalMixingAudioSource(clock=lambda: clk[0])
    mix.push_tts(_f32_frame(0.5, n=FRAME_SAMPLES))  # Marvin 正播長播報＝arm 前提
    mix.note_player_speech()                       # onset
    clk[0] = 0.5
    mix.note_player_speech()                       # 確認 → confirmed
    for _ in range(50):
        mix._tts_player_duck_step_toward(0.6)      # 玩家說話窗內
    assert abs(mix._tts_player_duck_cur - mix._tts_player_duck_level) < 1e-6  # → 10%


def test_tts_player_duck_restores_only_after_5s_silence_when_confirmed():
    clk = [0.0]
    mix = LocalMixingAudioSource(clock=lambda: clk[0])
    mix.push_tts(_f32_frame(0.5, n=FRAME_SAMPLES))  # Marvin 正播＝arm 前提
    mix.note_player_speech()                        # onset
    clk[0] = 0.1
    mix.note_player_speech()                        # 確認 → confirmed，until = 0.1 + 5
    for _ in range(50):
        mix._tts_player_duck_step_toward(3.0)       # 3s（<5.1s）→ 仍 duck
    assert mix._tts_player_duck_cur < 0.2           # 還壓著
    for _ in range(50):
        mix._tts_player_duck_step_toward(6.0)       # 6s（>5.1s 無聲）→ 回 1.0
    assert abs(mix._tts_player_duck_cur - 1.0) < 1e-6


def test_tts_player_duck_recovers_quickly_when_onset_not_confirmed():
    """用戶回饋：有時觸發只是雜音、不是真的講話——沒人接著說話要盡快拉回來，
    不該卡在整段 5s hold 裡才回滿。onset 沒被確認 → onset_hold_s（1s）後就該回 1.0。"""
    clk = [0.0]
    mix = LocalMixingAudioSource(clock=lambda: clk[0])
    mix.push_tts(_f32_frame(0.5, n=FRAME_SAMPLES))
    mix.note_player_speech()                        # onset only，until = 1.0
    for _ in range(50):
        mix._tts_player_duck_step_toward(2.0)       # 2s（>1s onset hold，沒被確認）→ 回 1.0
    assert abs(mix._tts_player_duck_cur - 1.0) < 1e-6


def test_tts_player_duck_no_change_when_no_speech():
    mix = LocalMixingAudioSource(clock=lambda: 100.0)
    for _ in range(20):
        mix._tts_player_duck_step_toward(100.0)
    assert mix._tts_player_duck_cur == 1.0          # 沒人說話 → 不 duck


def test_tts_output_ducked_when_player_speaking():
    """wiring：玩家持續說話（confirmed）+ duck 到位 → TTS 輸出 == apply_gain(tts, tts_gain * duck_level)。"""
    clk = [0.0]
    mix = LocalMixingAudioSource(seed=11, tts_gain=0.5, clock=lambda: clk[0])
    mix.push_tts(_f32_frame(0.6, n=FRAME_SAMPLES))        # Marvin 正播的長播報（就是被壓的這幀）
    mix.note_player_speech()                              # onset
    mix.note_player_speech()                              # 再次觸發（仍在 confirm window 內）→ confirmed
    mix._tts_player_duck_cur = mix._tts_player_duck_level  # 假設已 ramp 到位
    out = np.frombuffer(mix.read(), dtype=np.int16)
    tts = _f32_frame(0.6)
    expected = am.to_s16(am.tpdf_dither(
        am.apply_gain(tts, 0.5 * mix._tts_player_duck_level), np.random.default_rng(11)))
    assert np.array_equal(out, expected)


def test_tts_player_duck_resets_on_fresh_tts_when_no_speech():
    """idle 時 _cur 凍結在 duck 值 → 下一段 TTS（無人說話）onset 應復原 1.0，不殘留壓低新 TTS。"""
    clk = [100.0]
    mix = LocalMixingAudioSource(seed=1, clock=lambda: clk[0])
    mix._tts_player_duck_cur = 0.10        # 模擬 idle 凍結在 duck
    mix._player_speech_until = 0.0         # 無人說話（窗早過）
    mix.push_tts(_f32_frame(0.6, n=FRAME_SAMPLES))  # 新 TTS onset
    mix.read()                             # 一幀 → onset reset 應觸發
    assert mix._tts_player_duck_cur == 1.0


def test_tts_player_duck_keeps_duck_on_fresh_tts_while_player_speaking():
    """玩家正在說話 → 新 TTS onset 不復原（仍要 duck）。"""
    clk = [0.0]
    mix = LocalMixingAudioSource(seed=1, clock=lambda: clk[0])
    mix._tts_player_duck_cur = 0.10
    mix.push_tts(_f32_frame(0.6, n=FRAME_SAMPLES))  # Marvin 正播＝arm 前提
    mix.note_player_speech()               # barge-in → until = 5
    mix.push_tts(_f32_frame(0.6, n=FRAME_SAMPLES))
    mix.read()                             # onset 但窗內 → 不復原
    assert mix._tts_player_duck_cur < 0.5


# ── Wake Duck：喚醒確認 → 音樂 duck（不等 TTS）────────────────────────────────

def test_wake_duck_ducks_music_without_tts_then_restores():
    """duck_for_wake() → hold 內即使無 TTS 也 duck 到 _duck_level；hold 過後回 1.0。"""
    clock = [0.0]
    m = LocalMixingAudioSource(clock=lambda: clock[0])

    # 無 TTS、無 wake → 不 duck
    assert m._music_duck_target(False) == 1.0
    # 喚醒 → hold 內 duck（不等 TTS）
    m.duck_for_wake(hold_s=5.0)
    assert m._music_duck_target(False) == m._duck_level
    # hold 過後 → 回 1.0
    clock[0] = 6.0
    assert m._music_duck_target(False) == 1.0


def test_tts_active_ducks_regardless_of_wake():
    """TTS 播放中一定 duck（回話期間由 TTS duck 維持，與 wake duck 無關）。"""
    m = LocalMixingAudioSource(clock=lambda: 0.0)
    assert m._music_duck_target(True) == m._duck_level
