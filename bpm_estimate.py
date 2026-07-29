"""播放取樣估 BPM，落地存供未來選歌用鄰近節奏配歌（2026-07-28）。

沿用 loudness_norm.py 已在做的 25/50/75% 取樣基礎設施（ffmpeg 已經在解那三段
20s 音訊量響度），順便量 BPM——不上 librosa 等重依賴，用 numpy/scipy 已有的
onset autocorrelation 做粗估（找「差不多節奏」的鄰近歌，非精準節拍分析）。

純函式（估算/中位數/store 讀寫）放這檔，方便單測；實際 ffmpeg 取樣 PCM 在
cogs/music_cog.py 呼叫端。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

import memory_sandbox

MIN_BPM = 60.0
MAX_BPM = 200.0
_OCTAVE_LO = 70.0
_OCTAVE_HI = 180.0


def _normalize_octave(bpm: float, *, lo: float = _OCTAVE_LO, hi: float = _OCTAVE_HI) -> float:
    """把估計值摺回常見流行樂節奏帶，緩解（不保證消除）倍/半拍 octave error。"""
    while bpm < lo:
        bpm *= 2
    while bpm > hi:
        bpm /= 2
    return bpm


def estimate_bpm_from_pcm(pcm: np.ndarray, sr: int, *, min_bpm: float = MIN_BPM,
                          max_bpm: float = MAX_BPM, hop_s: float = 0.01) -> float | None:
    """單聲道 f32 PCM → 粗估 BPM。太短/全靜音/無明顯週期 → None。

    onset envelope（frame energy 一階差分，整流去均值）→ autocorrelation 在
    [min_bpm, max_bpm] 對應的 lag 範圍內抓峰值 → 換算 BPM → octave 摺回常見帶。
    """
    pcm = np.asarray(pcm, dtype=np.float32)
    if pcm.size < sr * 2:
        return None
    hop = max(1, int(sr * hop_s))
    frame = hop * 2
    n_frames = (pcm.size - frame) // hop
    if n_frames < 20:
        return None
    energy = np.array([
        np.sum(pcm[i * hop:i * hop + frame].astype(np.float64) ** 2)
        for i in range(n_frames)
    ])
    onset = np.diff(energy)
    onset = np.clip(onset, 0, None)
    if not np.any(onset):
        return None
    onset = onset - onset.mean()
    ac = np.correlate(onset, onset, mode="full")[len(onset) - 1:]
    if ac[0] <= 0:
        return None
    min_lag = max(1, int(round(60.0 / max_bpm / hop_s)))
    max_lag = min(len(ac) - 1, int(round(60.0 / min_bpm / hop_s)))
    if min_lag >= max_lag:
        return None
    segment = ac[min_lag:max_lag + 1]
    if segment.size == 0 or np.all(segment <= 0):
        return None
    peak_lag = min_lag + int(np.argmax(segment))
    if peak_lag <= 0:
        return None
    bpm = 60.0 / (peak_lag * hop_s)
    return _normalize_octave(bpm)


def median_bpm(values: list[float | None]) -> float | None:
    """多點取樣 BPM 估計的中位數（過濾 None）。全 None / 空 → None。"""
    nums = sorted(v for v in values if v is not None)
    if not nums:
        return None
    n = len(nums)
    mid = n // 2
    if n % 2:
        return nums[mid]
    return (nums[mid - 1] + nums[mid]) / 2.0


def read_bpm_store(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_bpm(path: str, video_id: str, bpm: float) -> None:
    """寫單一 videoId 的 BPM 估計（合併既有檔）。沙盒模式 no-op（ephemeral）。"""
    if memory_sandbox.active():
        return
    p = Path(path)
    store = read_bpm_store(path)
    store[video_id] = {"bpm": bpm, "ts": time.time()}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
