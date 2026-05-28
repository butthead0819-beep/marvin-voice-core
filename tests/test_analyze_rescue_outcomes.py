"""analyze_rescue_outcomes 離線分析 — daily ritual 看 rescue_outcomes.jsonl 用。

職責切分：
- load(path)    讀 jsonl → list[dict]
- analyze(rows) 純函式，輸入 rows 回分析 dict（容易單元測試）
- main()        實際 daily ritual entry point（CLI）

分析重點：
- by_gap_class：四分類計數，看整體分佈
- convergent_clusters：依 winner_agent + winner_reason 聚類，count ≥ 2 標 ready_to_propose
                       （使用者拍板的 intent gap 門檻，激進補 agent 偏好）
- divergent_by_target：依 pragmatic_target 分組看 signal 分佈，餵推薦扣分
- unmatched_samples：LLM 也救不回來的孤兒，落回 agent_gaps phase A.5 路徑
- shadow_samples：校準週用，人工檢視 LLM 改寫品質
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def analyze():
    from scripts.analyze_rescue_outcomes import analyze
    return analyze


@pytest.fixture
def load():
    from scripts.analyze_rescue_outcomes import load
    return load


def _row(**overrides):
    base = {
        "original_query": "下一首",
        "rewritten_query": "下一首",
        "winner_agent": "skip",
        "winner_reason": "skip",
        "pragmatic_signal": None,
        "pragmatic_target": None,
        "gap_class": "convergent",
        "speaker": "Alice",
        "ts": 0.0,
    }
    base.update(overrides)
    return base


# ── load() ────────────────────────────────────────────────────────────────────

def test_load_reads_jsonl_lines(tmp_path: Path, load):
    path = tmp_path / "rescue.jsonl"
    path.write_text(
        json.dumps(_row(gap_class="convergent")) + "\n"
        + json.dumps(_row(gap_class="divergent")) + "\n",
        encoding="utf-8",
    )
    rows = load(path)
    assert len(rows) == 2
    assert rows[0]["gap_class"] == "convergent"


def test_load_skips_blank_lines(tmp_path: Path, load):
    path = tmp_path / "rescue.jsonl"
    path.write_text(
        json.dumps(_row()) + "\n\n   \n" + json.dumps(_row()) + "\n",
        encoding="utf-8",
    )
    rows = load(path)
    assert len(rows) == 2


# ── gap_class 整體分佈 ───────────────────────────────────────────────────────

def test_analyze_counts_by_gap_class(analyze):
    rows = [
        _row(gap_class="convergent"),
        _row(gap_class="convergent"),
        _row(gap_class="divergent"),
        _row(gap_class="unmatched"),
        _row(gap_class="shadow"),
    ]
    result = analyze(rows)
    assert result["total"] == 5
    assert result["by_gap_class"]["convergent"] == 2
    assert result["by_gap_class"]["divergent"] == 1
    assert result["by_gap_class"]["unmatched"] == 1
    assert result["by_gap_class"]["shadow"] == 1


def test_analyze_handles_empty_rows(analyze):
    result = analyze([])
    assert result["total"] == 0
    assert result["by_gap_class"] == {}


# ── convergent clustering：regex 擴充提案來源 ────────────────────────────────

def test_convergent_cluster_groups_by_agent_and_reason(analyze):
    """同 winner_agent + winner_reason → 同一 cluster，原句進 samples。
    daily ritual 看 cluster 內句子就能提案 regex pattern。"""
    rows = [
        _row(gap_class="convergent", winner_agent="skip", winner_reason="skip",
             original_query="希望下次播放好聽的歌"),
        _row(gap_class="convergent", winner_agent="skip", winner_reason="skip",
             original_query="這首不太對"),
        _row(gap_class="convergent", winner_agent="volume", winner_reason="volume_down",
             original_query="能不能小聲一點"),
    ]
    result = analyze(rows)
    clusters = result["convergent_clusters"]
    by_key = {(c["winner_agent"], c["winner_reason"]): c for c in clusters}

    skip = by_key[("skip", "skip")]
    assert skip["count"] == 2
    assert "希望下次播放好聽的歌" in skip["samples"]
    assert "這首不太對" in skip["samples"]

    volume = by_key[("volume", "volume_down")]
    assert volume["count"] == 1


def test_convergent_cluster_marks_ready_to_propose_at_threshold_2(analyze):
    """使用者拍板：count ≥ 2 即 ready_to_propose（激進補 regex 偏好）。"""
    rows = [
        _row(gap_class="convergent", winner_agent="skip", winner_reason="skip",
             original_query="A"),
        _row(gap_class="convergent", winner_agent="skip", winner_reason="skip",
             original_query="B"),
        _row(gap_class="convergent", winner_agent="volume", winner_reason="volume_down",
             original_query="C"),  # count=1，不該 ready
    ]
    result = analyze(rows)
    by_key = {(c["winner_agent"], c["winner_reason"]): c for c in result["convergent_clusters"]}
    assert by_key[("skip", "skip")]["ready_to_propose"] is True
    assert by_key[("volume", "volume_down")]["ready_to_propose"] is False


def test_convergent_clusters_sorted_by_count_desc(analyze):
    """ops 看報告先看高頻 cluster，省 scroll。"""
    rows = (
        [_row(gap_class="convergent", winner_agent="a", winner_reason="r1",
              original_query=f"x{i}") for i in range(3)]
        + [_row(gap_class="convergent", winner_agent="b", winner_reason="r2",
                original_query=f"y{i}") for i in range(5)]
    )
    result = analyze(rows)
    counts = [c["count"] for c in result["convergent_clusters"]]
    assert counts == sorted(counts, reverse=True)


# ── divergent：餵推薦的 pragmatic 訊號 ───────────────────────────────────────

def test_divergent_grouped_by_pragmatic_target_and_signal(analyze):
    """daily ritual 看「current_song 收到幾次 negative」決定要不要扣分。"""
    rows = [
        _row(gap_class="divergent", pragmatic_signal="negative",
             pragmatic_target="current_song", original_query="這首不對"),
        _row(gap_class="divergent", pragmatic_signal="negative",
             pragmatic_target="current_song", original_query="不太好聽"),
        _row(gap_class="divergent", pragmatic_signal="positive",
             pragmatic_target="last_reply", original_query="說得好"),
    ]
    result = analyze(rows)
    current = result["divergent_by_target"]["current_song"]
    assert current["negative"]["count"] == 2
    assert "這首不對" in current["negative"]["samples"]

    last = result["divergent_by_target"]["last_reply"]
    assert last["positive"]["count"] == 1


# ── unmatched / shadow：人工檢視樣本 ────────────────────────────────────────

def test_unmatched_samples_capped_for_readability(analyze):
    """50 條 unmatched 全列出沒意義；只給人工看前 N（schema 預設 10）。"""
    rows = [_row(gap_class="unmatched", original_query=f"u{i}") for i in range(50)]
    result = analyze(rows)
    assert result["unmatched_total"] == 50
    assert len(result["unmatched_samples"]) <= 10


def test_shadow_samples_include_rewrite_and_signal(analyze):
    """shadow record 樣本要含 original→rewritten + signal —— 校準週要看 LLM 改寫品質。"""
    rows = [
        _row(gap_class="shadow", original_query="希望下次播放好聽的歌",
             rewritten_query="下一首", pragmatic_signal="negative",
             pragmatic_target="current_song"),
    ]
    result = analyze(rows)
    sample = result["shadow_samples"][0]
    assert sample["original_query"] == "希望下次播放好聽的歌"
    assert sample["rewritten_query"] == "下一首"
    assert sample["pragmatic_signal"] == "negative"
    assert sample["pragmatic_target"] == "current_song"


# ── 非 convergent record 不污染 convergent cluster ──────────────────────────

def test_other_gap_classes_excluded_from_convergent_clusters(analyze):
    """divergent / unmatched / shadow record 即使有 winner_agent，也不該進 convergent cluster。"""
    rows = [
        _row(gap_class="divergent", winner_agent="skip", winner_reason="skip"),
        _row(gap_class="shadow", winner_agent="skip", winner_reason="skip"),
        _row(gap_class="convergent", winner_agent="skip", winner_reason="skip",
             original_query="正常 convergent"),
    ]
    result = analyze(rows)
    clusters = result["convergent_clusters"]
    assert len(clusters) == 1
    assert clusters[0]["count"] == 1
