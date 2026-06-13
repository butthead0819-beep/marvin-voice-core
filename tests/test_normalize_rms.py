"""響度正規化（目標 RMS 制）— 取代固定 1.8x Golden Ear（2026-06-13）。

數據背景：近兩天 3,444 句 RMS 分布 p10=294 / 中位 2751 / p90=5330；
weakgogo（小聲講者）安靜時 STT 空白率 2.2% = 陳進文的 4 倍。
固定 1.8x + 2500 硬切讓 p10 等級的小聲音檔 boost 完仍只有 ~530。

設計：gain = clamp(target/rms, 1.0, max_gain)，再受峰值保護
（gain ≤ peak_ceiling/peak，寧可少 boost 不裁切失真）；
rms < min_rms 視為雜訊不放大。
"""
from __future__ import annotations

import numpy as np

from marvin_voice_core.audio_utils import normalize_rms


def _pcm(rms_target: float, n: int = 48000, freq: float = 440.0) -> bytes:
    """生成指定 RMS 的正弦 int16 PCM（單聲道排列即可，函式不管聲道）。"""
    t = np.arange(n) / 48000.0
    amp = rms_target * np.sqrt(2)  # 正弦 RMS = amp/√2
    return (np.sin(2 * np.pi * freq * t) * amp).astype(np.int16).tobytes()


def _rms(pcm: bytes) -> float:
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a ** 2)))


def test_quiet_input_boosted_to_target():
    out = normalize_rms(_pcm(800), target_rms=2800)
    assert abs(_rms(out) - 2800) / 2800 < 0.05


def test_very_quiet_noise_not_amplified():
    """低於 min_rms 視為雜訊，原樣返回（放大垃圾只會害 STT）。"""
    src = _pcm(50)
    out = normalize_rms(src, target_rms=2800, min_rms=100)
    assert out == src


def test_loud_input_unchanged():
    """已達標的不動（不衰減——大聲不是問題）。"""
    src = _pcm(4000)
    out = normalize_rms(src, target_rms=2800)
    assert out == src


def test_gain_capped():
    """p10 級小聲（294）：增益封頂 max_gain，不為了達標放大到失真。"""
    out = normalize_rms(_pcm(294), target_rms=2800, max_gain=6.0)
    assert _rms(out) < 294 * 6.0 * 1.05


def test_peak_protection_no_clipping():
    """尖峰訊號（高 peak 低 RMS）：增益受峰值保護，輸出無削波。"""
    a = np.zeros(48000, dtype=np.float32)
    a[::100] = 20000.0          # 稀疏尖峰 → RMS 低但 peak 高
    a += 200.0                  # 底噪墊高 RMS 過 min_rms
    src = a.astype(np.int16).tobytes()

    out = normalize_rms(src, target_rms=2800, peak_ceiling=30000)

    arr = np.frombuffer(out, dtype=np.int16)
    assert int(np.abs(arr.astype(np.int32)).max()) <= 30000


def test_output_same_length_and_dtype():
    src = _pcm(800, n=12345)
    out = normalize_rms(src, target_rms=2800)
    assert len(out) == len(src)
    assert np.frombuffer(out, dtype=np.int16).dtype == np.int16


def test_empty_input_passthrough():
    assert normalize_rms(b"", target_rms=2800) == b""
