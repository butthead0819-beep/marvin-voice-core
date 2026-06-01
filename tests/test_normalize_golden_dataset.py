"""normalize_golden_dataset — golden social-analysis 投影到最小共同 schema（蒸餾前正規化）。

最小共同 schema = {social_gap, confidence, sentiment}（2026-06-01 拍板 3-key）。
做三件事：(1) 統一欄位名 intervention_confidence→confidence (2) 修值型別
（pipe enum 取首 token、confidence clamp 0..1、sentiment 同義詞收斂、social_gap
同義詞收斂 info→information_backup 等）(3) 去重。

intervention 砍掉原因：最大變體（404 筆）無此欄、整體 65% null，留著只是弱訊號 +
null 雜訊。social_gap 收斂原因：縮寫版（info/redir/emo）與全名版是同概念，
不統一會教模型兩套標籤。社交分析以外、social_gap 缺漏、confidence 不可解析者 → drop。
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.normalize_golden_dataset import (
    canon_confidence,
    canon_sentiment,
    canon_social_gap,
    normalize_dataset,
    normalize_record,
)


def _rec(user="【現場】showay: hi", asst='{"social_gap": "none", "confidence": 0.8}'):
    return {"messages": [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": user},
        {"role": "assistant", "content": asst},
    ]}


# ── 欄位級 canon ──────────────────────────────────────────────────────────────

def test_canon_social_gap_splits_pipe_then_collapses_synonym():
    # pipe 取首 token "info" → 收斂成全名
    assert canon_social_gap("info|neutral|none") == "information_backup"


def test_canon_social_gap_collapses_abbreviations():
    assert canon_social_gap("info") == "information_backup"
    assert canon_social_gap("redir") == "subject_redirect"
    assert canon_social_gap("emo") == "emotional_support"
    # 全名版維持原樣（冪等）
    assert canon_social_gap("information_backup") == "information_backup"
    assert canon_social_gap("none") == "none"


def test_canon_social_gap_strips_and_missing():
    assert canon_social_gap("  redir ") == "subject_redirect"
    assert canon_social_gap("") is None
    assert canon_social_gap(None) is None


def test_canon_confidence_unifies_field_name():
    assert canon_confidence({"intervention_confidence": 0.0}) == 0.0
    assert canon_confidence({"confidence": 0.8}) == 0.8


def test_canon_confidence_clamps_and_coerces():
    assert canon_confidence({"confidence": "0.9"}) == 0.9
    assert canon_confidence({"confidence": 1.5}) == 1.0
    assert canon_confidence({"confidence": -0.2}) == 0.0
    assert canon_confidence({"topic": "x"}) is None  # 缺漏


def test_canon_sentiment_normalizes_synonyms():
    assert canon_sentiment("neg") == "negative"
    assert canon_sentiment("negative") == "negative"
    assert canon_sentiment("pos") == "positive"
    assert canon_sentiment(None) == "neutral"  # 缺漏 → 安全預設


# ── record 級 ─────────────────────────────────────────────────────────────────

def test_normalize_record_projects_to_minimal_3key_schema():
    rec = _rec(asst=json.dumps({
        "social_gap": "redir", "confidence": 0.8, "sentiment": "neg",
        "intervention_decision": "True", "minecraft_command": None,
        "suki_inner_monologue": "...", "topic": "chitchat",
    }, ensure_ascii=False))
    out = normalize_record(rec)
    asst = json.loads(out["messages"][-1]["content"])
    # 3-key、無 intervention、social_gap 收斂成全名
    assert asst == {"social_gap": "subject_redirect", "confidence": 0.8,
                    "sentiment": "negative"}


def test_normalize_record_keeps_system_and_user():
    out = normalize_record(_rec(user="原始輸入"))
    roles = [m["role"] for m in out["messages"]]
    assert roles == ["system", "user", "assistant"]
    assert out["messages"][1]["content"] == "原始輸入"


def test_normalize_record_drops_when_social_gap_missing():
    assert normalize_record(_rec(asst='{"confidence": 0.8}')) is None


def test_normalize_record_drops_freetext():
    assert normalize_record(_rec(asst="自由文本補位台詞")) is None


def test_normalize_record_drops_degenerate():
    assert normalize_record(_rec(asst="{}")) is None


# ── dataset 級：去重 ──────────────────────────────────────────────────────────

def test_normalize_dataset_dedups_identical_io():
    rows = [_rec(), _rec(), _rec(user="不同")]
    out, stats = normalize_dataset(rows)
    assert stats["written"] == 2
    assert stats["dropped_duplicate"] == 1


def test_normalize_dataset_stats_account_for_drops():
    rows = [
        _rec(asst='{"social_gap": "none", "confidence": 0.8}'),  # ok
        _rec(asst="自由文本"),                                     # freetext drop
        _rec(asst='{"confidence": 0.5}'),                         # missing social_gap drop
    ]
    out, stats = normalize_dataset(rows)
    assert stats["written"] == 1
    assert stats["dropped_unusable"] == 2
    assert len(out) == 1
