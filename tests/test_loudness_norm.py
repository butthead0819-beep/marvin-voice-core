"""每首歌響度正規化純函式測試（2026-06-04）。"""
from __future__ import annotations

import pytest

from loudness_norm import (
    MAX_GAIN, MIN_GAIN, TARGET_LUFS,
    average_lufs, compute_loudness_gain, parse_ebur128_integrated, sample_positions,
)


# ── compute_loudness_gain ─────────────────────────────────────────────────────

def test_gain_at_target_is_one():
    assert compute_loudness_gain(TARGET_LUFS) == pytest.approx(1.0)


def test_quiet_song_boosted():
    # -20 LUFS 比 -14 安靜 6dB → gain ≈ 2x
    assert compute_loudness_gain(-20.0) == pytest.approx(10 ** (6 / 20), rel=1e-6)
    assert compute_loudness_gain(-20.0) > 1.0


def test_loud_song_attenuated():
    # -8 LUFS 比 -14 大聲 → gain < 1
    assert compute_loudness_gain(-8.0) < 1.0


def test_gain_clamped_both_ends():
    assert compute_loudness_gain(-60.0) == MAX_GAIN     # 極安靜 → 不超過 MAX
    assert compute_loudness_gain(10.0) == MIN_GAIN      # 極大聲 → 不低於 MIN


def test_gain_none_is_unity():
    assert compute_loudness_gain(None) == 1.0           # 量測失敗不調


# ── sample_positions ──────────────────────────────────────────────────────────

def test_sample_positions_25_50_75():
    pos = sample_positions(200.0, window_s=20.0)
    assert pos == [50.0, 100.0, 150.0]


def test_sample_positions_clamps_to_avoid_tail_silence():
    # duration=50, window=20：75% = 37.5 > last_start(30) → clamp 到 30
    pos = sample_positions(50.0, window_s=20.0)
    assert pos[-1] == 30.0                      # 被 clamp，不超過 duration-window
    assert pos == [12.5, 25.0, 30.0]


def test_sample_positions_short_song_single_point():
    assert sample_positions(30.0, window_s=20.0) == [0.0]   # <2*window → 從頭量
    assert sample_positions(0.0) == [0.0]


# ── parse_ebur128_integrated ──────────────────────────────────────────────────

_EBUR128_TAIL = """\
[Parsed_ebur128_0 @ 0x55] Summary:

  Integrated loudness:
    I:         -16.3 LUFS
    Threshold: -26.5 LUFS
"""

def test_parse_ebur128_integrated():
    assert parse_ebur128_integrated(_EBUR128_TAIL) == -16.3


def test_parse_ebur128_takes_last():
    s = "I: -10.0 LUFS\nI: -16.3 LUFS"   # 最後一筆 = 整段整合
    assert parse_ebur128_integrated(s) == -16.3


def test_parse_ebur128_none_when_absent():
    assert parse_ebur128_integrated("no loudness here") is None
    assert parse_ebur128_integrated("") is None


# ── average_lufs ──────────────────────────────────────────────────────────────

def test_average_lufs_filters_none():
    assert average_lufs([-14.0, -16.0, None]) == pytest.approx(-15.0)


def test_average_lufs_all_none():
    assert average_lufs([None, None]) is None
