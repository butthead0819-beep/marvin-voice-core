"""TDD: 把一次打岔疊播的原始量測換算成可讀時機診斷。

目的是讓 log 能直接驗證「實際切入點 vs 設計尾段比例」，而非事後反推。
"""
from __future__ import annotations

import math

from manzai_interject import interject_diagnostics


def test_perceived_ratio_includes_first_chunk_latency():
    """Marmo 首塊延遲會把『耳朵聽到的切入點』往後推，比設計 at 更晚。"""
    d = interject_diagnostics(
        at_ratio=0.72, est_dur_s=10.5,
        marvin_frames=450, marmo_frames=322, marmo_first_chunk_s=0.6,
    )
    assert d["marvin_actual_s"] == 9.0          # 450 × 20ms
    assert d["marmo_actual_s"] == 6.44          # 322 × 20ms
    assert math.isclose(d["trigger_s"], 7.56, abs_tol=1e-6)        # 10.5 × 0.72
    assert math.isclose(d["perceived_entry_s"], 8.16, abs_tol=1e-6)  # 7.56 + 0.6
    # 設計 72% 但實際切入已逼近 91%
    assert math.isclose(d["perceived_ratio"], 8.16 / 9.0, abs_tol=1e-6)
    assert d["perceived_ratio"] > 0.90


def test_overlap_negative_when_marmo_enters_after_marvin_ends():
    """切入點晚到 Marvin 已講完 → overlap 為負（不是打斷，是接話）。"""
    d = interject_diagnostics(
        at_ratio=0.95, est_dur_s=10.0,
        marvin_frames=300, marmo_frames=200, marmo_first_chunk_s=1.0,
    )
    # trigger=9.5 +1.0 = 10.5s，marvin 實際只播 6.0s
    assert d["perceived_entry_s"] == 10.5
    assert d["overlap_s"] < 0


def test_higher_latency_pushes_entry_later():
    base = interject_diagnostics(
        at_ratio=0.72, est_dur_s=6.0,
        marvin_frames=300, marmo_frames=180, marmo_first_chunk_s=0.2,
    )
    slow = interject_diagnostics(
        at_ratio=0.72, est_dur_s=6.0,
        marvin_frames=300, marmo_frames=180, marmo_first_chunk_s=1.2,
    )
    assert slow["perceived_ratio"] > base["perceived_ratio"]
    assert math.isclose(slow["perceived_entry_s"] - base["perceived_entry_s"], 1.0, abs_tol=1e-6)


def test_zero_marvin_frames_is_safe():
    """無播放幀（極端 fallback）不該除零炸掉。"""
    d = interject_diagnostics(
        at_ratio=0.72, est_dur_s=0.0,
        marvin_frames=0, marmo_frames=0, marmo_first_chunk_s=0.0,
    )
    assert d["marvin_actual_s"] == 0.0
    assert d["perceived_ratio"] == 0.0
    assert d["overlap_s"] == 0.0
