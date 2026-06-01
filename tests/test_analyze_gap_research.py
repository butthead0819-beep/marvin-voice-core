"""analyze_gap_research — daily ritual 讀 gap_research.jsonl 量 shadow 成效。

每筆記錄 = 一次 pre-gate 放行後的 LLM 偵測。query 非 null = 命中真資訊真空。
shadow 一週後看 hit 數與 query 品質，決定是否值得建 Phase 2 交付。
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.analyze_gap_research import analyze, load


def _rec(query=None, mode="shadow", ts=0.0):
    return {"ts": ts, "mode": mode, "snippet": "s", "query": query, "delivered": False}


def test_load_skips_blank(tmp_path: Path):
    p = tmp_path / "g.jsonl"
    p.write_text(json.dumps(_rec()) + "\n\n" + json.dumps(_rec(query="x")) + "\n", encoding="utf-8")
    assert len(load(p)) == 2


def test_analyze_counts_hits_and_rate():
    rows = [_rec(), _rec(query="帳篷抗風"), _rec(query="M4 跑 72B"), _rec()]
    r = analyze(rows)
    assert r["escalations"] == 4
    assert r["gap_hits"] == 2
    assert r["hit_rate"] == 0.5
    assert "帳篷抗風" in r["sample_queries"]


def test_analyze_groups_by_mode():
    rows = [_rec(mode="shadow"), _rec(mode="shadow", query="q"), _rec(mode="live", query="q2")]
    r = analyze(rows)
    assert r["by_mode"]["shadow"] == 2
    assert r["by_mode"]["live"] == 1


def test_analyze_empty():
    r = analyze([])
    assert r["escalations"] == 0
    assert r["hit_rate"] == 0.0
    assert r["sample_queries"] == []
