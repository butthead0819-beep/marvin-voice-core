"""48kHz stereo → 16kHz mono 抗混疊降頻（2026-06-13）。

背景：engine 原本用 `mean(axis=1)[::3]` 裸抽取，無抗混疊低通——
8kHz 以上能量（齒音/音樂殘響/雜訊）摺疊回語音頻帶變失真，
影響 Whisper/Groq/雅婷/Gemini shadow 全部下游 lane（Swift 不受影響，
它吃原生 48k WAV）。改用 windowed-sinc FIR 低通 + 3:1 抽取（純 numpy，
不加 scipy 依賴）。

驗收核心：帶外音（>8kHz）必須被強烈衰減，帶內音與輸出契約不變。
"""
from __future__ import annotations

import numpy as np

from marvin_voice_core.audio_utils import pcm48k_stereo_to_16k_mono


def _stereo_pcm_from_tone(freq_hz: float, seconds: float = 1.0, amp: float = 0.5) -> bytes:
    """產生 48kHz stereo int16 PCM 的單音測試訊號（左右聲道相同）。"""
    t = np.arange(int(48000 * seconds)) / 48000.0
    mono = (np.sin(2 * np.pi * freq_hz * t) * amp * 32767).astype(np.int16)
    return np.column_stack([mono, mono]).reshape(-1).tobytes()


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0


# ── 輸出契約（與舊 inline 版本相同，下游 lane 零改動）──────────────────────

def test_output_is_16k_mono_float32_in_range():
    pcm = _stereo_pcm_from_tone(1000.0, seconds=0.5)
    out = pcm48k_stereo_to_16k_mono(pcm)

    assert out.dtype == np.float32
    assert len(out) == 8000  # 0.5s × 16kHz
    assert np.max(np.abs(out)) <= 1.0


def test_empty_input_returns_empty_array():
    out = pcm48k_stereo_to_16k_mono(b"")
    assert isinstance(out, np.ndarray)
    assert len(out) == 0


def test_stereo_channels_are_averaged():
    """左 +x、右 -x → mono 平均應近零。"""
    t = np.arange(4800) / 48000.0
    left = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16)
    pcm = np.column_stack([left, -left]).reshape(-1).tobytes()

    out = pcm48k_stereo_to_16k_mono(pcm)

    assert _rms(out) < 0.01


# ── 抗混疊（本次修正的核心）────────────────────────────────────────────────

def test_in_band_tone_preserved():
    """1kHz（語音帶內）能量必須保留（衰減 < 2dB）。"""
    pcm = _stereo_pcm_from_tone(1000.0, amp=0.5)
    out = pcm48k_stereo_to_16k_mono(pcm)

    # 掐頭去尾避開濾波器暫態
    core = out[800:-800]
    assert _rms(core) > 0.5 * 0.794 / np.sqrt(2)  # amp 0.5 正弦 RMS ≈ 0.354，容忍 -2dB


def test_above_nyquist_tone_strongly_attenuated():
    """12kHz（> 目標 Nyquist 8kHz）裸抽取會混疊成 4kHz 全量保留；
    抗混疊版必須砍到 < 入帶能量的 10%。"""
    pcm = _stereo_pcm_from_tone(12000.0, amp=0.5)
    out = pcm48k_stereo_to_16k_mono(pcm)

    core = out[800:-800]
    naive_rms = 0.5 / np.sqrt(2)  # 裸抽取下混疊音的 RMS（能量不滅）
    assert _rms(core) < naive_rms * 0.10


def test_aliasing_regression_vs_naive():
    """直接對照：同一段 12kHz 訊號，新版輸出能量必須遠小於裸抽取版。"""
    pcm = _stereo_pcm_from_tone(12000.0, amp=0.5)
    arr = np.frombuffer(pcm, dtype=np.int16).reshape(-1, 2)
    naive = arr.mean(axis=1)[::3].astype(np.float32) / 32768.0

    out = pcm48k_stereo_to_16k_mono(pcm)

    assert _rms(out[800:-800]) < _rms(naive[800:-800]) * 0.15
