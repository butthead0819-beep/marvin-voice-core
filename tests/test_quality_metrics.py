"""TDD: quality_metrics.py — Marvin 四指標 capture + 聚合管道（Phase 1）。

capture ≠ aggregate：事件當下 append jsonl（容錯不 raise），每日聚合成 rate/p50/p95。
Phase 1 只驗管道 + false-responding；latency/percentile 骨架給 Phase 2 react_ms 用。
"""
from __future__ import annotations

import pytest

from quality_metrics import (
    record_metric, read_metrics, percentile,
    summarize_false_responding, summarize_latency, summarize_interruption,
    summarize_recall,
)


_BASE = 1_779_000_000.0


def _log(tmp_path):
    return tmp_path / "qm.jsonl"


# ── record / read round-trip ──────────────────────────────────────────────

def test_record_then_read_roundtrip(tmp_path):
    p = _log(tmp_path)
    record_metric("false_responding", path=p, clock=lambda: _BASE,
                  speaker="大肚", was_false=True, reason="empty_harvest")
    rows = read_metrics(p)
    assert len(rows) == 1
    assert rows[0]["metric"] == "false_responding"
    assert rows[0]["speaker"] == "大肚"
    assert rows[0]["was_false"] is True
    assert rows[0]["ts"] == _BASE


def test_record_never_raises_on_unwritable_path(tmp_path):
    # path 的父是一個檔案 → mkdir/open 失敗，但 record 不得 raise（熱路徑安全）
    afile = tmp_path / "afile"
    afile.write_text("x", encoding="utf-8")
    record_metric("react", path=afile / "sub" / "qm.jsonl", react_ms=120.0)  # 不應 raise


def test_read_missing_file_returns_empty(tmp_path):
    assert read_metrics(tmp_path / "nope.jsonl") == []


def test_read_skips_corrupt_lines(tmp_path):
    p = _log(tmp_path)
    p.write_text(
        '{"ts": 1, "metric": "react", "react_ms": 100}\n'
        'GARBAGE NOT JSON\n'
        '\n'
        '{"ts": 2, "metric": "react", "react_ms": 200}\n',
        encoding="utf-8")
    rows = read_metrics(p)
    assert len(rows) == 2
    assert [r["react_ms"] for r in rows] == [100, 200]


def test_read_filters_by_metric(tmp_path):
    p = _log(tmp_path)
    record_metric("react", path=p, react_ms=100)
    record_metric("false_responding", path=p, was_false=False)
    assert len(read_metrics(p, metric="react")) == 1
    assert read_metrics(p, metric="react")[0]["metric"] == "react"


def test_read_filters_by_time_window(tmp_path):
    p = _log(tmp_path)
    record_metric("react", path=p, clock=lambda: _BASE, react_ms=1)
    record_metric("react", path=p, clock=lambda: _BASE + 100, react_ms=2)
    record_metric("react", path=p, clock=lambda: _BASE + 200, react_ms=3)
    rows = read_metrics(p, since_ts=_BASE + 100, until_ts=_BASE + 200)
    assert [r["react_ms"] for r in rows] == [2]   # since 含、until 不含


# ── false-responding rate ─────────────────────────────────────────────────

def test_false_responding_rate(tmp_path):
    p = _log(tmp_path)
    for wf in (True, False, False, False):   # 1 false / 4 total = 0.25
        record_metric("false_responding", path=p, was_false=wf)
    s = summarize_false_responding(read_metrics(p))
    assert s["total"] == 4
    assert s["false"] == 1
    assert s["false_rate"] == pytest.approx(0.25)


def test_false_responding_empty_is_zero():
    s = summarize_false_responding([])
    assert s == {"total": 0, "false": 0, "false_rate": 0.0}


def test_false_responding_ignores_other_metrics(tmp_path):
    p = _log(tmp_path)
    record_metric("react", path=p, react_ms=100)            # 不該被算進 false-responding
    record_metric("false_responding", path=p, was_false=True)
    s = summarize_false_responding(read_metrics(p))
    assert s["total"] == 1 and s["false"] == 1


# ── percentile / latency（Phase 2 react_ms 骨架）───────────────────────────

def test_percentile_empty_is_zero():
    assert percentile([], 50) == 0.0


def test_percentile_median_and_p95():
    vals = list(range(1, 101))   # 1..100
    assert percentile(vals, 50) == pytest.approx(50.5)
    assert percentile(vals, 95) == pytest.approx(95.05)


def test_summarize_latency_stats(tmp_path):
    p = _log(tmp_path)
    for ms in (100, 200, 300):
        record_metric("react", path=p, react_ms=ms)
    s = summarize_latency(read_metrics(p, metric="react"))
    assert s["count"] == 3
    assert s["mean"] == pytest.approx(200.0)
    assert s["p50"] == pytest.approx(200.0)


def test_summarize_latency_empty():
    s = summarize_latency([])
    assert s == {"count": 0, "p50": 0.0, "p95": 0.0, "mean": 0.0}


# ── daily report（純函式 build_report）─────────────────────────────────────

def test_build_report_shows_false_rate():
    from scripts.quality_metrics_report import build_report
    rows = [
        {"metric": "false_responding", "was_false": True},
        {"metric": "false_responding", "was_false": False},
        {"metric": "false_responding", "was_false": False},
        {"metric": "false_responding", "was_false": False},
    ]
    out = build_report(rows, "2026-05-22")
    assert "2026-05-22" in out
    assert "25.0%" in out          # 1/4 false
    assert "wakes: 4" in out


def test_build_report_react_placeholder_when_no_data():
    from scripts.quality_metrics_report import build_report
    out = build_report([], "2026-05-22")
    assert "Phase 2" in out        # react_ms 尚未 instrument
    assert "無 Track-B wake" in out


def test_build_report_shows_interruption_rate():
    from scripts.quality_metrics_report import build_report
    rows = [
        {"metric": "interruption", "interrupted": True, "was_playing": False},
        {"metric": "interruption", "interrupted": False, "was_playing": False},
    ]
    out = build_report(rows, "2026-05-22")
    assert "打斷率: 50.0%" in out
    assert "idle-only" in out


def test_build_report_react_stats_when_present():
    from scripts.quality_metrics_report import build_report
    rows = [{"metric": "react", "react_ms": 200}, {"metric": "react", "react_ms": 400}]
    out = build_report(rows, "2026-05-22")
    assert "p50:" in out and "mean:" in out


def test_day_bounds_is_24h_window():
    from scripts.quality_metrics_report import day_bounds
    since, until, label = day_bounds("2026-05-22")
    assert until - since == 86400.0
    assert label == "2026-05-22"


# ── Phase 2 接線契約：LatencyMarks → react_ms ─────────────────────────────

# ── bad-timing interruption ────────────────────────────────────────────────

def test_interruption_rate(tmp_path):
    p = _log(tmp_path)
    for it in (True, True, False, False, False):   # 2 打斷 / 5 開口 = 0.4
        record_metric("interruption", path=p, interrupted=it, was_playing=False)
    s = summarize_interruption(read_metrics(p))
    assert s["total"] == 5
    assert s["interrupted"] == 2
    assert s["interrupt_rate"] == pytest.approx(0.4)


def test_interruption_idle_only_excludes_echo_suspect(tmp_path):
    """was_playing=True 的開口（Marvin 已在播，user_is_speaking 可能是回聲）→ idle_only 排除。"""
    p = _log(tmp_path)
    record_metric("interruption", path=p, interrupted=True, was_playing=True)   # echo 嫌疑
    record_metric("interruption", path=p, interrupted=True, was_playing=False)  # 乾淨打斷
    record_metric("interruption", path=p, interrupted=False, was_playing=False)
    s = summarize_interruption(read_metrics(p), idle_only=True)
    assert s["total"] == 2          # 只算 was_playing=False
    assert s["interrupted"] == 1


def test_interruption_empty_is_zero():
    s = summarize_interruption([])
    assert s == {"total": 0, "interrupted": 0, "interrupt_rate": 0.0}


# ── recall（weekly probe）──────────────────────────────────────────────────

def test_recall_accuracy(tmp_path):
    p = _log(tmp_path)
    for ok in (True, True, True, False):   # 3/4 = 0.75
        record_metric("recall", path=p, correct=ok)
    s = summarize_recall(read_metrics(p))
    assert s["total"] == 4
    assert s["correct"] == 3
    assert s["accuracy"] == pytest.approx(0.75)


def test_recall_empty_is_zero():
    s = summarize_recall([])
    assert s == {"total": 0, "correct": 0, "accuracy": 0.0}


def test_latency_total_feeds_react_ms(tmp_path):
    """接點契約：play_tts 的 mark_first_audio dict 的 total_wake_to_audio_ms 即 react_ms。
    鎖定 react time 定義＝wake hit → first audio（使用者聽到開口）。"""
    from latency_tracker import LatencyMarks
    p = _log(tmp_path)
    m = LatencyMarks()
    m.mark_wake("大肚", 1000.0)
    m.mark_llm_start(1000.5)
    m.mark_first_sentence(1001.0)
    stage2 = m.mark_first_audio_and_consume(1002.0)   # 2.0s 後聽到 → 2000ms
    record_metric("react", path=p, speaker=stage2["speaker"],
                  react_ms=round(stage2["total_wake_to_audio_ms"], 1),
                  tts_ms=round(stage2["sentence_to_audio_ms"], 1))
    s = summarize_latency(read_metrics(p, metric="react"))
    assert s["count"] == 1
    assert s["p50"] == pytest.approx(2000.0)
