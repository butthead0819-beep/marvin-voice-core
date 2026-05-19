"""Tests for FM vs Groq cleaner harness pure logic.

設計範圍：
  - strip_json_fences: 把 ```json ... ``` 包裹剝掉
  - parse_cleaner_response: 解析 cleaner JSON，欄位型別驗證，失敗回 None
  - compare_outputs: 兩邊 cleaner 結果分類（agree / cleaned_diff / wake_diff / parse_fail）
  - aggregate_report: 多列匯總統計

不測 subprocess 與 Groq HTTP（留給 integration smoke）。
"""
from __future__ import annotations

import pytest

from scripts.fm_vs_groq_harness import (
    CleanerResult,
    ComparisonRow,
    aggregate_report,
    compare_outputs,
    parse_cleaner_response,
    strip_json_fences,
)


# ── strip_json_fences ─────────────────────────────────────────────────────────

def test_strip_json_fences_removes_fenced_block():
    text = '```json\n{"cleaned": "嗨"}\n```'
    assert strip_json_fences(text) == '{"cleaned": "嗨"}'


def test_strip_json_fences_removes_plain_fence():
    text = '```\n{"cleaned": "嗨"}\n```'
    assert strip_json_fences(text) == '{"cleaned": "嗨"}'


def test_strip_json_fences_passes_through_unfenced():
    text = '{"cleaned": "嗨"}'
    assert strip_json_fences(text) == '{"cleaned": "嗨"}'


def test_strip_json_fences_handles_trailing_text():
    text = '```json\n{"cleaned": "嗨"}\n```\n說明：略'
    out = strip_json_fences(text)
    assert out.startswith('{') and out.endswith('}')


# ── parse_cleaner_response ────────────────────────────────────────────────────

def test_parse_cleaner_response_valid_json():
    out = parse_cleaner_response(
        '{"cleaned": "馬文你好", "intent": 0.9, "calling": true, "is_complete": true}'
    )
    assert out is not None
    assert out.cleaned == "馬文你好"
    assert out.intent == 0.9
    assert out.calling is True
    assert out.is_complete is True


def test_parse_cleaner_response_strips_fences():
    out = parse_cleaner_response(
        '```json\n{"cleaned": "嗨", "intent": 0.0, "calling": false, "is_complete": true}\n```'
    )
    assert out is not None
    assert out.cleaned == "嗨"


def test_parse_cleaner_response_clamps_intent():
    out = parse_cleaner_response(
        '{"cleaned": "嗨", "intent": 1.5, "calling": false, "is_complete": true}'
    )
    assert out is not None and out.intent == 1.0


def test_parse_cleaner_response_rejects_wrong_type_intent():
    # FM 常吐 intent 變字串 — 視為 schema 違反，視為 parse fail
    out = parse_cleaner_response(
        '{"cleaned": "嗨", "intent": "high", "calling": true, "is_complete": true}'
    )
    assert out is None


def test_parse_cleaner_response_rejects_wrong_type_calling():
    out = parse_cleaner_response(
        '{"cleaned": "嗨", "intent": 0.5, "calling": "yes", "is_complete": true}'
    )
    assert out is None


def test_parse_cleaner_response_rejects_missing_cleaned():
    out = parse_cleaner_response(
        '{"intent": 0.5, "calling": false, "is_complete": true}'
    )
    assert out is None


def test_parse_cleaner_response_rejects_malformed_json():
    assert parse_cleaner_response("not json at all") is None
    assert parse_cleaner_response("") is None


# ── compare_outputs ───────────────────────────────────────────────────────────

def _r(cleaned="嗨", intent=0.0, calling=False, is_complete=True):
    return CleanerResult(cleaned=cleaned, intent=intent, calling=calling, is_complete=is_complete)


def test_compare_outputs_full_agreement():
    fm = _r(cleaned="馬文你好", intent=0.9, calling=True)
    groq = _r(cleaned="馬文你好", intent=0.9, calling=True)
    row = compare_outputs(raw="馬文你好", fm=fm, groq=groq, fm_latency_ms=100, groq_latency_ms=200)
    assert row.cleaned_agree is True
    assert row.wake_decision_agree is True
    assert row.fm_parse_ok is True
    assert row.groq_parse_ok is True


def test_compare_outputs_cleaned_disagree():
    fm = _r(cleaned="馬文你好", intent=0.9, calling=True)
    groq = _r(cleaned="馬文，你好", intent=0.9, calling=True)
    row = compare_outputs(raw="麻文你好", fm=fm, groq=groq, fm_latency_ms=100, groq_latency_ms=200)
    assert row.cleaned_agree is False
    assert row.wake_decision_agree is True  # intent 都 ≥0.7 → 兩邊都 wake


def test_compare_outputs_wake_decision_disagree():
    # FM 判 wake，Groq 沒有
    fm = _r(cleaned="馬文你好", intent=0.9, calling=True)
    groq = _r(cleaned="馬文你好", intent=0.3, calling=False)
    row = compare_outputs(raw="麻文你好", fm=fm, groq=groq, fm_latency_ms=100, groq_latency_ms=200)
    assert row.wake_decision_agree is False


def test_compare_outputs_fm_parse_fail():
    row = compare_outputs(raw="嗨", fm=None, groq=_r(), fm_latency_ms=100, groq_latency_ms=200)
    assert row.fm_parse_ok is False
    assert row.cleaned_agree is False
    assert row.wake_decision_agree is False


def test_compare_outputs_both_parse_fail():
    row = compare_outputs(raw="嗨", fm=None, groq=None, fm_latency_ms=100, groq_latency_ms=200)
    assert row.fm_parse_ok is False
    assert row.groq_parse_ok is False


# ── aggregate_report ──────────────────────────────────────────────────────────

def test_aggregate_report_computes_percentages():
    rows = [
        ComparisonRow(raw="a", cleaned_agree=True, wake_decision_agree=True,
                      fm_parse_ok=True, groq_parse_ok=True,
                      fm_latency_ms=100, groq_latency_ms=200,
                      fm_cleaned="a", groq_cleaned="a", fm_intent=0.0, groq_intent=0.0),
        ComparisonRow(raw="b", cleaned_agree=False, wake_decision_agree=True,
                      fm_parse_ok=True, groq_parse_ok=True,
                      fm_latency_ms=150, groq_latency_ms=250,
                      fm_cleaned="b1", groq_cleaned="b2", fm_intent=0.0, groq_intent=0.0),
        ComparisonRow(raw="c", cleaned_agree=False, wake_decision_agree=False,
                      fm_parse_ok=False, groq_parse_ok=True,
                      fm_latency_ms=300, groq_latency_ms=200,
                      fm_cleaned=None, groq_cleaned="c", fm_intent=None, groq_intent=0.5),
    ]
    report = aggregate_report(rows)
    assert report["n"] == 3
    assert report["fm_parse_success_rate"] == pytest.approx(2 / 3, abs=0.01)
    assert report["groq_parse_success_rate"] == 1.0
    # cleaned_agreement / wake_agreement 只計算 FM+Groq 都 parse 成功的 row
    # both_ok = [row a, row b]：cleaned 1/2，wake 2/2（row b wake_agree=True）
    assert report["cleaned_agreement"] == pytest.approx(1 / 2, abs=0.01)
    assert report["wake_decision_agreement"] == pytest.approx(2 / 2, abs=0.01)
    # 延遲統計：p50/p95 over all rows
    assert report["fm_latency_p50_ms"] == 150
    assert report["groq_latency_p50_ms"] == 200


def test_aggregate_report_classifies_verdict_pass():
    # cleaned ≥85% + wake ≥90% + FM p95 ≤ Groq p95 → switch
    rows = [
        ComparisonRow(raw=f"r{i}", cleaned_agree=True, wake_decision_agree=True,
                      fm_parse_ok=True, groq_parse_ok=True,
                      fm_latency_ms=100, groq_latency_ms=200,
                      fm_cleaned="x", groq_cleaned="x", fm_intent=0.0, groq_intent=0.0)
        for i in range(20)
    ]
    report = aggregate_report(rows)
    assert report["verdict"] == "switch"


def test_aggregate_report_classifies_verdict_wake_veto_only():
    # cleaned 70-85% + wake ≥90% → wake_veto_only
    rows = []
    for i in range(20):
        cleaned_agree = i < 15  # 75%
        rows.append(ComparisonRow(
            raw=f"r{i}", cleaned_agree=cleaned_agree, wake_decision_agree=True,
            fm_parse_ok=True, groq_parse_ok=True,
            fm_latency_ms=100, groq_latency_ms=200,
            fm_cleaned="x", groq_cleaned="x", fm_intent=0.0, groq_intent=0.0,
        ))
    report = aggregate_report(rows)
    assert report["verdict"] == "wake_veto_only"


def test_aggregate_report_classifies_verdict_reject():
    # cleaned <70% → reject
    rows = []
    for i in range(20):
        rows.append(ComparisonRow(
            raw=f"r{i}", cleaned_agree=(i < 10), wake_decision_agree=(i < 10),
            fm_parse_ok=True, groq_parse_ok=True,
            fm_latency_ms=100, groq_latency_ms=200,
            fm_cleaned="x", groq_cleaned="x", fm_intent=0.0, groq_intent=0.0,
        ))
    report = aggregate_report(rows)
    assert report["verdict"] == "reject"


def test_aggregate_report_rejects_when_fm_slower():
    # cleaned ≥85% + wake ≥90% 但 FM p95 > Groq p95 → reject（記憶 #1 切換條件）
    rows = []
    for i in range(20):
        rows.append(ComparisonRow(
            raw=f"r{i}", cleaned_agree=True, wake_decision_agree=True,
            fm_parse_ok=True, groq_parse_ok=True,
            fm_latency_ms=500, groq_latency_ms=200,
            fm_cleaned="x", groq_cleaned="x", fm_intent=0.0, groq_intent=0.0,
        ))
    report = aggregate_report(rows)
    assert report["verdict"] == "reject"
