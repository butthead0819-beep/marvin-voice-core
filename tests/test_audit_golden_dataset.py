"""audit_golden_dataset — suki_golden_dataset.jsonl 健康度 audit（蒸餾前置 gate）。

為什麼是 audit 而非 replay-eval（2026-06-01 發現）：
golden 的 social-analysis output schema 飄移嚴重（15+ 欄位組合）、值髒
（string bool `"True"`、pipe enum `"info|neutral|none"`）、混入退化樣本（`{}`、
`{"type":"object"}`）與 `__META__` 污染。在正規化前 replay 比對等於拿髒 ground
truth 當基準（feedback_audit_data_purity / feedback_mock_dont_self_fixture 兩雷）。
v1 先純函式 audit，輸出「可蒸餾比例」verdict，零 LLM、可進 3am batch。
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.audit_golden_dataset import (
    audit,
    classify_record,
    count_exact_duplicates,
    flag_dirty,
    load,
)


def _rec(user="【現場原文】showay: hi", assistant='{"social_gap": "none", "confidence": 0.8}'):
    return {
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": user},
            {"role": "assistant", "content": assistant},
        ],
        "timestamp": "2026-04-04T16:50:42",
    }


# ── load：必須容忍壞行（golden 有退化/壞 JSON），不能讓 batch 崩 ──────────────

def test_load_skips_blank_and_malformed_lines(tmp_path: Path):
    p = tmp_path / "g.jsonl"
    p.write_text(
        json.dumps(_rec()) + "\n\n  \n" + "{not json\n" + json.dumps(_rec()) + "\n",
        encoding="utf-8",
    )
    rows = load(p)
    assert len(rows) == 2  # 空行 + 壞行被跳過，不丟例外


# ── classify_record：五分類 ───────────────────────────────────────────────────

def test_classify_social_analysis():
    rec = _rec(assistant='{"social_gap": "redir", "confidence": 0.8}')
    assert classify_record(rec) == "social_analysis"


def test_classify_freetext_gap_filling():
    rec = _rec(assistant="你還記得那次酒吧的趣事嗎？")
    assert classify_record(rec) == "freetext"


def test_classify_degenerate_empty_object():
    assert classify_record(_rec(assistant="{}")) == "degenerate"


def test_classify_degenerate_type_skeleton():
    assert classify_record(_rec(assistant='{"type": "object"}')) == "degenerate"


def test_classify_polluted_meta_leak():
    rec = _rec(user='大肚: __META__ {"avg_confidence":0.36}')
    assert classify_record(rec) == "polluted"


def test_classify_unparseable_no_assistant():
    rec = {"messages": [{"role": "user", "content": "hi"}], "timestamp": "t"}
    assert classify_record(rec) == "unparseable"


# ── flag_dirty：髒值偵測（只對 social_analysis 的 parsed dict）─────────────────

def test_flag_dirty_string_bool():
    flags = flag_dirty({"social_gap": "none", "intervention_decision": "True", "confidence": 0.8})
    assert "string_bool" in flags


def test_flag_dirty_pipe_enum():
    flags = flag_dirty({"social_gap": "info|neutral|none", "confidence": 0.8})
    assert "pipe_enum" in flags


def test_flag_dirty_field_name_drift():
    """舊 prompt 用 intervention_confidence，新版改 confidence — 視為 drift。"""
    flags = flag_dirty({"social_gap": "none", "intervention_confidence": 0.0})
    assert "field_name_drift" in flags


def test_flag_dirty_missing_social_gap():
    flags = flag_dirty({"confidence": 0.8, "topic": "chitchat"})
    assert "missing_social_gap" in flags


def test_flag_dirty_clean_record_has_no_flags():
    flags = flag_dirty({"social_gap": "none", "confidence": 0.8, "sentiment": "neutral"})
    assert flags == set()


# ── count_exact_duplicates：完全重複的 messages = 污染 ─────────────────────────

def test_count_exact_duplicates():
    rows = [_rec(), _rec(), _rec(user="不同輸入")]
    # 3 筆中 2 筆完全相同 → 1 筆重複
    assert count_exact_duplicates(rows) == 1


# ── audit：聚合 verdict ───────────────────────────────────────────────────────

def test_audit_categories_and_usable_ratio():
    rows = [
        _rec(assistant='{"social_gap": "none", "confidence": 0.8}'),   # clean social
        _rec(assistant='{"social_gap": "info|x", "confidence": 0.8}'), # dirty (pipe)
        _rec(assistant="自由文本補位"),                                  # freetext
        _rec(assistant="{}"),                                          # degenerate
        _rec(user='__META__ leak'),                                    # polluted
    ]
    result = audit(rows)
    assert result["total"] == 5
    assert result["categories"]["social_analysis"] == 2
    assert result["categories"]["freetext"] == 1
    assert result["categories"]["degenerate"] == 1
    assert result["categories"]["polluted"] == 1
    # 只有 1 筆 social_analysis 是乾淨可蒸餾的
    assert result["social_analysis"]["clean_usable"] == 1
    assert result["verdict"]["distillation_ready"] == 1


def test_audit_empty():
    result = audit([])
    assert result["total"] == 0
    assert result["verdict"]["distillation_ready"] == 0
    assert result["verdict"]["usable_ratio"] == 0.0
