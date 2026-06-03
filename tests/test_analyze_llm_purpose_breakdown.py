"""TDD: 每日 LLM 呼叫歸因報表 pure core。

承 #1/#2/#3：per-purpose 量、cleaner 截斷救援率、過矯正次數。
"""
from __future__ import annotations

import datetime as _dt

from scripts.analyze_llm_purpose_breakdown import (
    aggregate_by_purpose,
    build_report,
    categorize_error,
    count_cleaner_events,
)


def _ts(day: str, hour: int = 12) -> float:
    d = _dt.date.fromisoformat(day)
    return _dt.datetime(d.year, d.month, d.day, hour, 0, 0).timestamp()


# ── categorize_error ──────────────────────────────────────────────────────────

def test_categorize_error_buckets():
    assert categorize_error("") == "ok"
    assert categorize_error(None) == "ok"
    assert categorize_error("RateLimitError: 429 quota") == "429_限流"
    assert categorize_error("Model llama3.1-8b does not exist") == "404_模型下架"
    assert categorize_error("503 server error") == "5xx_server"
    assert categorize_error("no_llm_available: all below threshold") == "no_llm_池冷卻"
    assert categorize_error("asyncio TimeoutError") == "timeout"
    assert categorize_error("某種怪錯") == "other"


# ── aggregate_by_purpose ──────────────────────────────────────────────────────

def test_aggregate_groups_by_purpose_and_filters_day():
    day = "2026-06-02"
    rows = [
        {"ts": _ts(day), "purpose": "generate_greeting", "success": True},
        {"ts": _ts(day), "purpose": "generate_greeting", "success": False, "error": "429"},
        {"ts": _ts(day), "purpose": "extract_memory", "success": True},
        {"ts": _ts("2026-06-01"), "purpose": "generate_greeting", "success": True},  # 別天，濾掉
    ]
    agg = aggregate_by_purpose(rows, day)
    assert agg["generate_greeting"]["ok"] == 1
    assert agg["generate_greeting"]["fail"] == 1
    assert agg["generate_greeting"]["reasons"]["429_限流"] == 1
    assert agg["extract_memory"]["ok"] == 1
    # 別天那筆不計入
    assert agg["generate_greeting"]["ok"] + agg["generate_greeting"]["fail"] == 2


def test_aggregate_ignores_rows_without_ts():
    agg = aggregate_by_purpose([{"purpose": "x", "success": True}], "2026-06-02")
    assert agg == {}


# ── count_cleaner_events ──────────────────────────────────────────────────────

def test_count_cleaner_events_by_day():
    day = "2026-06-02"
    lines = [
        f"{day} 00:01:00,000 [INFO] stt_cleaner: 🔧 [STT Clean] JSON 截斷，救回 cleaned: 'abc'",
        f"{day} 00:02:00,000 [WARNING] stt_cleaner: ⚠️ [STT Clean] JSON 解析失敗，降級純文字: x",
        f"{day} 00:03:00,000 [WARNING] stt_cleaner: ⚠️ [STT Clean] LLM 注入喚醒詞 (過矯正)：'Siri' -> '馬文'",
        "2026-06-01 23:00:00,000 [INFO] stt_cleaner: 🔧 [STT Clean] JSON 截斷，救回 cleaned: 'old'",  # 別天
    ]
    out = count_cleaner_events(lines, day)
    assert out == {"recovered": 1, "json_failed": 1, "overcorrection": 1}


def test_count_cleaner_events_empty():
    assert count_cleaner_events([], "2026-06-02") == {
        "recovered": 0, "json_failed": 0, "overcorrection": 0}


# ── build_report ──────────────────────────────────────────────────────────────

def test_build_report_no_data_is_honest():
    rep = build_report([], [], "2026-06-02")
    assert "不捏造" in rep
    assert "2026-06-02" in rep


def test_build_report_marks_background_and_recovery_rate():
    day = "2026-06-02"
    rows = [
        {"ts": _ts(day), "purpose": "extract_memory", "success": True},
        {"ts": _ts(day), "purpose": "generate_fast_response", "success": True},
    ]
    lines = [
        f"{day} 00:01:00,000 stt_cleaner: 🔧 [STT Clean] JSON 截斷，救回 cleaned: 'a'",
        f"{day} 00:02:00,000 stt_cleaner: 🔧 [STT Clean] JSON 截斷，救回 cleaned: 'b'",
        f"{day} 00:03:00,000 stt_cleaner: ⚠️ [STT Clean] JSON 解析失敗，降級純文字: x",
    ]
    rep = build_report(rows, lines, day)
    assert "extract_memory" in rep
    assert "✅" in rep                  # 背景標記
    assert "救援率 67%" in rep          # 2 救回 / (2+1)
