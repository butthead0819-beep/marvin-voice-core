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

from local_mixing_source import (
    LocalMixingAudioSource,
    MixerPlaybackAdapter,
    S16ToF32MusicSource,
    ensure_mixer_playing,
    FRAME_SAMPLES,
    FRAME_BYTES_S16,
    SAMPLE_RATE,
    CHANNELS,
)

import audio_mixing as am


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


# ── ensure_mixer_playing ─────────────────────────────────────────────────────

def _vc(connected=True, playing=False):
    vc = MagicMock()
    vc.is_connected.return_value = connected
    vc.is_playing.return_value = playing
    return vc


def test_ensure_playing_plays_when_idle_vc():
    mix = LocalMixingAudioSource()
    vc = _vc(connected=True, playing=False)
    assert ensure_mixer_playing(vc, lambda: MixerPlaybackAdapter(mix)) is True
    assert vc.play.call_count == 1
    assert isinstance(vc.play.call_args.args[0], MixerPlaybackAdapter)


def test_ensure_playing_idempotent_when_already_playing():
    mix = LocalMixingAudioSource()
    vc = _vc(connected=True, playing=True)
    assert ensure_mixer_playing(vc, lambda: MixerPlaybackAdapter(mix)) is False
    assert not vc.play.called


def test_ensure_playing_no_vc():
    mix = LocalMixingAudioSource()
    assert ensure_mixer_playing(None, lambda: MixerPlaybackAdapter(mix)) is False


def test_ensure_playing_not_connected():
    mix = LocalMixingAudioSource()
    vc = _vc(connected=False, playing=False)
    assert ensure_mixer_playing(vc, lambda: MixerPlaybackAdapter(mix)) is False
    assert not vc.play.called


def test_ensure_playing_swallows_already_playing_race():
    mix = LocalMixingAudioSource()
    vc = _vc(connected=True, playing=False)
    vc.play.side_effect = RuntimeError("Already playing audio")  # TOCTOU race
    assert ensure_mixer_playing(vc, lambda: MixerPlaybackAdapter(mix)) is False  # 不 raise


def test_ensure_playing_fresh_adapter_each_call():
    mix = LocalMixingAudioSource()
    seen = []
    factory = lambda: MixerPlaybackAdapter(mix)  # noqa: E731
    ensure_mixer_playing(_vc(playing=False), lambda: seen.append(factory()) or seen[-1])
    ensure_mixer_playing(_vc(playing=False), lambda: seen.append(factory()) or seen[-1])
    assert len(seen) == 2 and seen[0] is not seen[1]  # 每次新 adapter，不重用
