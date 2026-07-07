"""Plan 12 純 DSP — gain / f32 mix-sum / TPDF dither / s16 clip。

零 I/O、純 numpy、shape-agnostic（不綁 frame size / channels）。
由 offline A/B script（scripts/plan12_offline_ab.py）與 live
LocalMixingAudioSource 共用，讓 ±2 LSB 音質驗證字面覆蓋 live DSP。

Plan 12 核心：增益在量化前（f32）發生，等同「把 volume 烤進 ffmpeg」的音質
卻能即時調。pipeline：apply_gain → mix_layers → tpdf_dither → to_s16。
"""
from __future__ import annotations

import numpy as np

_LSB = np.float32(1.0 / 32768.0)


def apply_gain(frame: np.ndarray, gain: float) -> np.ndarray:
    """f32 frame × gain（量化前增益）。"""
    return frame * np.float32(gain)


def peak_normalize_f32(frame: np.ndarray, target_peak: float = 0.9) -> np.ndarray:
    """把 f32 音訊峰值正規化到 target_peak 滿幅。

    ack mp3 本身振幅偏低（mean ~-25dB），進 mixer 再 ×tts_gain 會被 ducked 音樂蓋掉。
    先拉到一致響度再送出。全靜音（peak=0）原樣回傳，不除以零。
    """
    if frame.size == 0:
        return frame
    peak = float(np.max(np.abs(frame)))
    if peak <= 0.0:
        return frame
    return frame * np.float32(target_peak / peak)


def mix_layers(layers: list[np.ndarray]) -> np.ndarray:
    """逐元素相加多個同形 f32 layer（音樂 + TTS overlay）。"""
    acc = layers[0]
    for layer in layers[1:]:
        acc = acc + layer
    return acc


def tpdf_dither(frame: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """三角機率密度 dither：兩個獨立 Uniform(-0.5,+0.5) LSB 相加 = 三角分布 [-1,+1] LSB。

    rng 由 caller 提供：offline 用 seeded（可重現），live mixer 用長命 Generator。
    """
    d1 = rng.uniform(-0.5, 0.5, size=frame.shape).astype(np.float32)
    d2 = rng.uniform(-0.5, 0.5, size=frame.shape).astype(np.float32)
    return frame + (d1 + d2) * _LSB


def to_s16(frame: np.ndarray) -> np.ndarray:
    """f32 [-1,1] → s16，clip 不 wrap（overflow 夾到 ±32767/−32768）。"""
    return np.clip(np.round(frame * 32768.0), -32768, 32767).astype(np.int16)
