"""Plan 12 Phase A kill-gate：非喚醒語句價值窗分析（2026-06-13）。

eng review Finding 2：(c) 串流早切只對「夠長」的非喚醒 turn 有價值——太短的句子
VAD 已先切，daemon（deferred-start 要先讓 wake-check 窗過）根本來不及。決策 gate：
若落在價值窗的非喚醒 turn 比例太低 → 不做 Phase B。

純函式分析 records/volatile_shadow.jsonl（schema: wake_first_ms / audio_ms / error）。
"""
from __future__ import annotations

from scripts.analyze_volatile_value_window import (
    analyze_audit_window,
    analyze_value_window,
    parse_audit_durations,
)


def _rec(audio_ms, wake_first_ms=None, error=None):
    return {"audio_ms": audio_ms, "wake_first_ms": wake_first_ms, "error": error}


def test_non_wake_only_counted():
    """喚醒 turn（wake_first_ms 非 None）不算進非喚醒分母。"""
    rows = [
        _rec(2000, wake_first_ms=600),  # 喚醒，排除
        _rec(2000),                      # 非喚醒，在窗
        _rec(500),                       # 非喚醒，太短
    ]
    out = analyze_value_window(rows, min_ms=1800)
    assert out["non_wake_total"] == 2
    assert out["in_window"] == 1
    assert out["in_window_pct"] == 50.0


def test_error_rows_skipped():
    rows = [_rec(2000, error="boom"), _rec(2000)]
    out = analyze_value_window(rows, min_ms=1800)
    assert out["non_wake_total"] == 1


def test_value_window_threshold_respected():
    rows = [_rec(1799), _rec(1800), _rec(5000)]
    out = analyze_value_window(rows, min_ms=1800)
    assert out["in_window"] == 2  # 1800 與 5000，不含 1799


def test_empty_safe():
    out = analyze_value_window([], min_ms=1800)
    assert out["non_wake_total"] == 0
    assert out["in_window_pct"] == 0.0


def test_verdict_kill_when_below_threshold():
    """價值窗比例 < gate → verdict 建議不做 Phase B（min_samples=1 隔離 verdict 邏輯）。"""
    rows = [_rec(500), _rec(600), _rec(700), _rec(2000)]  # 1/4 = 25%
    out = analyze_value_window(rows, min_ms=1800, gate_pct=30.0, min_samples=1)
    assert out["in_window_pct"] == 25.0
    assert out["verdict"] == "skip_phase_b"


def test_verdict_proceed_when_above_threshold():
    rows = [_rec(2000), _rec(2500), _rec(3000), _rec(500)]  # 3/4 = 75%
    out = analyze_value_window(rows, min_ms=1800, gate_pct=30.0, min_samples=1)
    assert out["verdict"] == "proceed_phase_b"


def test_verdict_insufficient_data():
    """樣本太少 → 不下結論（避免 9 筆就拍板）。"""
    rows = [_rec(2000), _rec(2500)]
    out = analyze_value_window(rows, min_ms=1800, gate_pct=30.0, min_samples=30)
    assert out["verdict"] == "insufficient_data"


# ── Audio Audit log 高量數據源（歷史句長，每句都記）──────────────────────────

def test_parse_audit_durations_extracts_ms():
    lines = [
        "2026-06-13 10:00:00 [INFO] 📊 [Audio Audit] User_1 | RMS: 300 -> 1800 | 長度: 1.84s",
        "noise line",
        "2026-06-13 10:00:01 [INFO] 📊 [Audio Audit] User_2 | RMS: 500 | 長度: 0.78s",
    ]
    out = parse_audit_durations(lines)
    assert out == [1840.0, 780.0]


def test_audit_window_proceed_when_enough_long():
    durs = [1840.0, 2500.0, 4000.0, 500.0]  # 3/4 ≥1800
    out = analyze_audit_window(durs, min_ms=1800, gate_pct=30.0, min_samples=1)
    assert out["in_window_pct"] == 75.0
    assert out["verdict"] == "proceed_phase_b"


def test_audit_window_skip_when_mostly_short():
    durs = [400.0, 600.0, 800.0, 2000.0]  # 1/4 ≥1800
    out = analyze_audit_window(durs, min_ms=1800, gate_pct=30.0, min_samples=1)
    assert out["verdict"] == "skip_phase_b"
