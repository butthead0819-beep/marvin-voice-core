"""笑聲節律啟發式（不用 ML）：在 RMS 包絡上量「規律的能量爆發」。

洞察：「哈-哈-哈-哈」= 約 4-7 Hz 的規律能量爆發；講話包絡較不規律。
純函式、只做算術——給離線驗證 + 之後 sink 整合共用（sink.write 熱路徑要 sync 要快）。
"""
from __future__ import annotations

import math

# 初始猜測門檻（離線 probe 會校）
LAUGH_RATE_LO = 3.0     # 每秒爆發數下限
LAUGH_RATE_HI = 9.0     # 上限
LAUGH_REGULARITY = 0.5  # 間隔規律度下限（1=完全等距）
LAUGH_MIN_BURSTS = 3


def rms_envelope(samples, frame_len: int) -> list[float]:
    """把 mono 取樣切成 frame、回每 frame 的 RMS（能量包絡）。"""
    out = []
    n = len(samples)
    for i in range(0, n - frame_len + 1, frame_len):
        seg = samples[i:i + frame_len]
        if not len(seg):
            continue
        acc = 0.0
        for s in seg:
            acc += float(s) * float(s)
        out.append(math.sqrt(acc / len(seg)))
    return out


def rhythm_features(envelope, frame_rate_hz: float) -> dict:
    """包絡 → {bursts, peaks_per_sec, regularity}。

    bursts = 越過動態門檻（平均能量）的上行次數；regularity = 1 - 間隔變異係數。
    """
    n = len(envelope)
    if n < 2 or frame_rate_hz <= 0:
        return {"bursts": 0, "peaks_per_sec": 0.0, "regularity": 0.0}
    thr = sum(envelope) / n
    cross = []  # 上行越界的 frame index
    for i in range(1, n):
        if envelope[i - 1] < thr <= envelope[i]:
            cross.append(i)
    duration = n / frame_rate_hz
    peaks_per_sec = len(cross) / duration if duration > 0 else 0.0
    if len(cross) >= 2:
        gaps = [cross[i] - cross[i - 1] for i in range(1, len(cross))]
        mean_gap = sum(gaps) / len(gaps)
        if mean_gap > 0:
            var = sum((g - mean_gap) ** 2 for g in gaps) / len(gaps)
            cv = math.sqrt(var) / mean_gap
            regularity = max(0.0, min(1.0, 1.0 - cv))
        else:
            regularity = 0.0
    else:
        regularity = 0.0
    return {"bursts": len(cross), "peaks_per_sec": peaks_per_sec, "regularity": regularity}


def looks_like_laugh(features: dict) -> bool:
    """節律特徵是否落在笑聲帶。"""
    return (features.get("bursts", 0) >= LAUGH_MIN_BURSTS
            and LAUGH_RATE_LO <= features.get("peaks_per_sec", 0.0) <= LAUGH_RATE_HI
            and features.get("regularity", 0.0) >= LAUGH_REGULARITY)
