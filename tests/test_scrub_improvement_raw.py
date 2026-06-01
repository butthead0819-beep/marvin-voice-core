"""scrub_improvement_raw — 寬放 ZDR：judge/gaps/rescue 的 raw 原文過 TTL 轉單向 hash。

設計核心（為何 hash 而非清空）：
analyze_agent_gaps 的 distinct 計數 key on raw_query。直接清空會讓所有舊記錄塌成
同一 key、汙染 distinct/plan-trigger。改存 sha1 指紋 → 可讀原文消失（ZDR 達標），
但相同原文 → 相同 hash，distinct/dedup 相等性保留，分析不失準。
代價：>TTL 舊資料無法再語意 clustering（clustering 跑近期累積，TTL 給足窗口）。

冪等：已 hash（值帶 scrubbed: 前綴）的記錄重跑不再 re-hash。
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.scrub_improvement_raw import (
    SCRUB_PREFIX,
    scrub_rows,
    scrub_value,
)


def _row(ts: float, **fields):
    return {"ts": ts, "speaker": "Alice", **fields}


# ── scrub_value：單向 hash，相等性保留、冪等 ──────────────────────────────────

def test_scrub_value_preserves_equality():
    """相同原文 → 相同指紋（distinct 計數才不會壞）。"""
    a = scrub_value("再跟馬文要")
    b = scrub_value("再跟馬文要")
    c = scrub_value("不同的話")
    assert a == b
    assert a != c
    assert a.startswith(SCRUB_PREFIX)


def test_scrub_value_removes_readable_text():
    """指紋不得包含原文（ZDR）。"""
    out = scrub_value("機密原話內容")
    assert "機密" not in out
    assert "原話" not in out


def test_scrub_value_idempotent():
    """已 hash 的值再 scrub 不變（重跑安全）。"""
    once = scrub_value("hello")
    twice = scrub_value(once)
    assert once == twice


# ── scrub_rows：只動 ts < cutoff 的記錄、只動指定欄位 ─────────────────────────

def test_scrub_only_records_older_than_cutoff():
    rows = [
        _row(ts=100.0, raw_query="舊原話"),   # 過期 → 該 scrub
        _row(ts=900.0, raw_query="新原話"),   # 未過期 → 保留
    ]
    out, n = scrub_rows(rows, text_fields=["raw_query"], cutoff_ts=500.0)
    assert n == 1
    assert out[0]["raw_query"].startswith(SCRUB_PREFIX)
    assert out[1]["raw_query"] == "新原話"  # 近期原文完整保留供 clustering


def test_scrub_multiple_fields():
    rows = [_row(ts=100.0, original_query="原", rewritten_query="改寫")]
    out, _ = scrub_rows(rows, text_fields=["original_query", "rewritten_query"], cutoff_ts=500.0)
    assert out[0]["original_query"].startswith(SCRUB_PREFIX)
    assert out[0]["rewritten_query"].startswith(SCRUB_PREFIX)


def test_scrub_preserves_derived_fields():
    """非文字欄位（derived）完全不動。"""
    rows = [_row(ts=100.0, raw_query="原話", intent_type="set_alarm", confidence=0.8)]
    out, _ = scrub_rows(rows, text_fields=["raw_query"], cutoff_ts=500.0)
    assert out[0]["intent_type"] == "set_alarm"
    assert out[0]["confidence"] == 0.8


def test_scrub_distinct_equality_survives():
    """同原文的兩筆過期記錄 → scrub 後仍相等（distinct 計數不壞）。"""
    rows = [
        _row(ts=100.0, raw_query="同一句"),
        _row(ts=200.0, raw_query="同一句"),
    ]
    out, _ = scrub_rows(rows, text_fields=["raw_query"], cutoff_ts=500.0)
    assert out[0]["raw_query"] == out[1]["raw_query"]


def test_scrub_skips_missing_or_empty_field():
    rows = [_row(ts=100.0), _row(ts=100.0, raw_query="")]
    out, n = scrub_rows(rows, text_fields=["raw_query"], cutoff_ts=500.0)
    assert n == 0  # 無欄位 / 空字串都不算 scrub


def test_scrub_idempotent_on_rerun():
    rows = [_row(ts=100.0, raw_query="原話")]
    once, n1 = scrub_rows(rows, text_fields=["raw_query"], cutoff_ts=500.0)
    twice, n2 = scrub_rows(once, text_fields=["raw_query"], cutoff_ts=500.0)
    assert n1 == 1
    assert n2 == 0  # 第二次沒有可 scrub 的
    assert once[0]["raw_query"] == twice[0]["raw_query"]
