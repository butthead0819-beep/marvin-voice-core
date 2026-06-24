"""crosstalk_track / pick_hottest_window / _crosstalk_events 行為測試。

夜晚回放秀 v0.1：把單峰 crosstalk_peak 泛化成整窗時間序列，給 EKG 用。
crosstalk_peak 行為必須逐筆不變（curator.py 依賴）。
"""
from __future__ import annotations

import pytest

from diary_comic.crosstalk import (
    CrosstalkPeak,
    _crosstalk_events,
    activity_track,
    crosstalk_peak,
    crosstalk_track,
    pick_hottest_window,
)

# 各 ≥ MIN_SUB(8) 字的長句
L_A = "今天天氣真的很好啊啊"      # 10
L_B = "對啊我也這樣覺得欸欸"      # 10
L_C = "欸欸欸欸欸欸欸欸欸欸欸"    # 11
L_D = "哈哈哈哈哈哈哈哈哈哈"      # 10
L_E = "真的真的真的真的真的真的"  # 12
SHORT = "嗯嗯"                    # 2 < MIN_SUB


# ── _crosstalk_events ──────────────────────────────────────────────
def test_events_empty_returns_empty():
    assert _crosstalk_events([]) == []


def test_events_all_short_filtered():
    rows = [("A", SHORT, 100.0), ("B", SHORT, 100.5)]
    assert _crosstalk_events(rows) == []


def test_events_single_speaker_none():
    rows = [("A", L_A, 100.0), ("A", L_E, 101.0)]
    assert _crosstalk_events(rows) == []  # 只有一個人 → 無 group≥2


def test_events_two_person_overlap_one_event():
    rows = [("A", L_A, 100.0), ("B", L_B, 101.0)]
    events = _crosstalk_events(rows)
    assert len(events) == 1
    assert events[0].speakers == ["A", "B"]
    assert events[0].ts == 100.0


def test_events_gap_boundary_splits():
    rows = [("A", L_A, 100.0), ("B", L_B, 103.0)]  # gap 3 > GAP 2
    assert _crosstalk_events(rows) == []  # 沒人在別人講話時插入


# ── crosstalk_peak 回歸（逐筆不變）─────────────────────────────────
def _multi_event_rows():
    # event1: A+B @100 (2 人, chars 20)
    # event2: C+D+A @200 (3 人, chars 11+10+12=33) ← 最熱
    return [
        ("A", L_A, 100.0),
        ("B", L_B, 101.0),
        ("C", L_C, 200.0),
        ("D", L_D, 200.5),
        ("A", L_E, 201.0),
    ]


def test_peak_regression_unchanged():
    peak = crosstalk_peak(_multi_event_rows())
    assert peak is not None
    assert peak.ts == 200.0
    assert peak.speakers == ["A", "C", "D"]      # sorted
    assert peak.heat == pytest.approx(3 + min(33 / 300.0, 0.9))
    assert peak.lines[0] == ("C", L_C)           # 事件起點是 C


def test_peak_first_max_wins_on_tie():
    # 兩個等熱 2 人事件，回傳「最先」那個（與舊 `>` 行為一致）
    rows = [
        ("A", L_A, 100.0), ("B", L_B, 101.0),     # event @100
        ("C", L_A, 300.0), ("D", L_B, 301.0),     # event @300，同 chars 同人數 → 同 heat
    ]
    peak = crosstalk_peak(rows)
    assert peak.ts == 100.0


def test_peak_no_events_none():
    assert crosstalk_peak([("A", SHORT, 100.0)]) is None


# ── crosstalk_track ────────────────────────────────────────────────
def test_track_empty():
    assert crosstalk_track([]) == []


def test_track_bin_takes_max_not_sum():
    # 同一 bin 內兩事件 → 取 max，不是 sum
    rows = _multi_event_rows()
    track = crosstalk_track(rows, bin_s=10.0)
    # 含 ts=200 的 bin 應該是 event2 的 heat（3.11），非 200+200.5 兩事件相加
    hot = max(h for _, h in track)
    assert hot == pytest.approx(3 + min(33 / 300.0, 0.9))
    # 不會超過單一事件最大值（排除相加）
    assert hot < 4.0


def test_track_bin_assignment_separates_distant_events():
    rows = _multi_event_rows()
    track = crosstalk_track(rows, bin_s=10.0)
    hot_bins = [t for t, h in track if h > 0]
    # @100 與 @200 必須落在不同 bin
    assert any(t < 150 for t in hot_bins)
    assert any(t >= 150 for t in hot_bins)


# ── activity_track（發言密度＝熱鬧）─────────────────────────────────
def test_activity_track_empty():
    assert activity_track([]) == []


def test_activity_track_counts_per_bin():
    # 短句也算（不像 crosstalk 要 ≥8 字）；bin_s=30
    rows = [("A", "x", 100.0), ("B", "y", 105.0), ("A", "z", 130.0)]
    track = activity_track(rows, bin_s=30.0)
    assert track[0] == (100.0, 2.0)   # bin [100,130) → 2 句
    assert track[1] == (130.0, 1.0)   # bin [130,160) → 1 句


def test_activity_track_lively_night_not_mostly_zero():
    # 對照 crosstalk：輪流講的熱鬧夜，activity 連續、crosstalk 幾乎全零
    rows = [("A" if i % 2 else "B", "今天天氣真好欸", 100.0 + i * 3) for i in range(40)]
    act = activity_track(rows, bin_s=30.0)
    cross = crosstalk_track(rows, bin_s=30.0)
    act_nz = sum(1 for _, h in act if h > 0)
    cross_nz = sum(1 for _, h in cross if h > 0)
    assert act_nz >= cross_nz          # 發言密度抓到的熱鬧 ≥ 搶話


# ── pick_hottest_window ────────────────────────────────────────────
def test_pick_window_flat_returns_none():
    assert pick_hottest_window([], win_s=120.0) is None


def test_pick_window_selects_around_peak():
    rows = _multi_event_rows()
    track = crosstalk_track(rows, bin_s=10.0)
    win = pick_hottest_window(track, win_s=60.0)
    assert win is not None
    start, end = win
    assert start <= 200.0 <= end       # 最熱點 @200 在窗內
    assert end - start == pytest.approx(60.0)
