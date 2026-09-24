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

# 所有 Marvin 講話（TTS / 預錄口白 / ack 音檔）進 TTS 層前統一過這條濾鏡：
# speechnorm 把各聲線/語調（深夜 -15%、親密 -35%…）拉到同一水位 → 壓縮提高密度 →
# 限幅 -2 dBFS 留空間給 duck 後的音樂相加（mix 最後是硬 clip）。
# 2026-09-24 ebur128 實測：YunJhe/HsiaoChen/Ryan/親密 -35% 皆 -14.4±0.7 LUFS（對齊音樂
# 正規化目標 -14），短句與 say 備援 ±2；串流首幀 +~30ms。原始 edge-tts 只有 -22 LUFS。
TTS_LOUDNESS_AF = (
    "speechnorm=e=12.5:r=0.0001:l=1,"
    "acompressor=threshold=0.125:ratio=6:attack=3:release=60:makeup=3,"
    "alimiter=limit=0.79:level=false"
)


def dj_mix_filter_complex(vol: float, dj_gain: float) -> str:
    """DJ Mix（口白 input 0 疊在歌 input 1 上）的 ffmpeg filter_complex。

    口白先過 TTS_LOUDNESS_AF（跟其他 TTS 同規則），音量 dj_gain（caller 傳
    calculate_tts_gain(vol)）；sidechain 用正規化後的口白偵測來 duck 音樂。原本口白
    寫死 30% 沒正規化，車上（音樂滿幅）聽不到（2026-09-24）。
    """
    return (
        f"[0:a]{TTS_LOUDNESS_AF},asplit=2[dj_sc][dj_raw];"
        f"[dj_sc]apad=whole_dur=9999[dj_pad];"
        f"[dj_raw]volume={dj_gain:.3f}[dj_q];"
        f"[1:a]loudnorm=I=-14:TP=-1.5:LRA=11,volume={vol:.3f}[music];"
        f"[music][dj_pad]sidechaincompress=threshold=0.02:ratio=8:attack=5:release=600[ducked];"
        f"[ducked][dj_q]amix=inputs=2:duration=longest:normalize=0[out]"
    )


# mixer 輸出最後一道防線（2026-09-24 使用者：寧願悶或小聲也絕對不能超大音量）。
# 源頭（TTS 濾鏡 / 音樂每首增益）都控好時不會觸發；觸發代表某個源頭漏了。
LIMIT_CEILING = 0.89            # -1 dBFS
LIMIT_RELEASE_PER_FRAME = 0.02  # 每幀（20ms）增益最多回升 0.02 → 0.5→1.0 約 0.5s


def limit_frame(frame: np.ndarray, prev_gain: float, *,
                ceiling: float = LIMIT_CEILING,
                release: float = LIMIT_RELEASE_PER_FRAME) -> tuple[np.ndarray, float, bool]:
    """逐幀峰值限幅：壓下去立刻、回升慢；幀內從 prev_gain 線性 ramp 到新增益（防 click），
    ramp 前段仍超過的樣本最後硬夾到 ceiling（寧悶勿爆）。回 (out, new_gain, 是否觸發)。"""
    if frame.size == 0:
        return frame, prev_gain, False
    pk = float(np.max(np.abs(frame)))
    target = min(1.0, ceiling / pk) if pk > 0 else 1.0
    new_gain = target if target < prev_gain else min(target, prev_gain + release)
    if prev_gain >= 1.0 and new_gain >= 1.0:
        return frame, 1.0, False
    if frame.size % 2 == 0:
        ramp = np.linspace(prev_gain, new_gain, frame.size // 2, dtype=np.float32).repeat(2)
    else:
        ramp = np.linspace(prev_gain, new_gain, frame.size, dtype=np.float32)
    out = np.clip(frame * ramp, -ceiling, ceiling)
    return out.astype(np.float32, copy=False), new_gain, target < 1.0


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
