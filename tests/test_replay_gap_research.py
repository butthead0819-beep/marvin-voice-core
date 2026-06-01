"""replay_gap_research — 離線去風險：用歷史語料量 pre-gate 通過率與 LLM 命中率。

目的：在串接 live voice_controller 前，先離線回答兩個風險問題——
(1) pre-gate 會放多少句去燒 LLM（成本/量）(2) 升級後多少回 query vs NONE（誤報率）。
本檔測純聚合；loader / LLM 呼叫是 IO shell。
"""
from __future__ import annotations

from gap_research import ResearchRequest
from scripts.replay_gap_research import pregate_stats, summarize_detections


def test_pregate_stats_counts_pass_rate():
    utterances = ["到底是多少？", "倒立洗頭", "為什麼會這樣", "我喜歡這首歌"]
    s = pregate_stats(utterances)
    assert s["total"] == 4
    assert s["passed"] == 2  # 含疑問訊號的兩句
    assert s["pass_rate"] == 0.5


def test_pregate_stats_empty():
    s = pregate_stats([])
    assert s["total"] == 0
    assert s["pass_rate"] == 0.0


def test_summarize_detections_hit_rate_and_samples():
    results = [
        ResearchRequest(query="帳篷抗風", snippet="x"),
        None,
        ResearchRequest(query="M4 跑 72B", snippet="y"),
    ]
    s = summarize_detections(results)
    assert s["evaluated"] == 3
    assert s["gap_hits"] == 2
    assert s["hit_rate"] == round(2 / 3, 4)
    assert "帳篷抗風" in s["sample_queries"]


def test_summarize_detections_empty():
    s = summarize_detections([])
    assert s["evaluated"] == 0
    assert s["hit_rate"] == 0.0
    assert s["sample_queries"] == []
