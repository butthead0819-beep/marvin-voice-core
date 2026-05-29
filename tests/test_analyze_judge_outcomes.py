"""analyze_judge_outcomes.analyze() — None-confidence 容錯回歸。

Bug（2026-05-30 發現）：judge 的 confidence=None 時，disagree 分支的
f"{conf:.2f}" 炸 TypeError，整個 daily ritual judge 分析 crash。

修法：safe formatter，None → "?"（不假裝成 0.00，那會混淆真實的 0 信心）。
"""
from __future__ import annotations

from scripts.analyze_judge_outcomes import analyze


def _row(j1_conf, j3_conf, *, j1_name="music", j3_name="guard", raw="播歌"):
    return {
        "raw_query": raw,
        "winning_judge": "j1_regex",
        "winner_name": j1_name,
        "winner_confidence": j1_conf if j1_conf is not None else 0,
        "judges": [
            {"name": "j1_regex", "bid_name": j1_name, "confidence": j1_conf,
             "latency_ms": 1.0, "bid_reason": "r1"},
            {"name": "j3_cleaner_precomputed", "bid_name": j3_name, "confidence": j3_conf,
             "latency_ms": 5.0, "bid_reason": "r3"},
        ],
    }


def test_analyze_does_not_crash_when_one_judge_confidence_is_none():
    """j1 有信心、j3=None 且 bid_name 不同 → 進 disagree 分支 → 舊版炸 format。"""
    rows = [_row(j1_conf=0.5, j3_conf=None)]
    result = analyze(rows)  # 不該拋 TypeError
    assert result["total"] == 1


def test_disagree_entry_renders_none_confidence_as_placeholder():
    """None 信心要顯示成 '?'，不是 0.00（避免跟真實 0 信心混淆）。"""
    rows = [_row(j1_conf=0.5, j3_conf=None, j1_name="music", j3_name="guard")]
    result = analyze(rows)
    disagree = result["j1_j3_disagree"]
    assert len(disagree) == 1
    # j3 的 None 信心應呈現為 ?，j1 的 0.5 正常
    assert "guard(?)" in disagree[0]["j3"]
    assert "music(0.50)" in disagree[0]["j1"]


def test_both_none_confidence_counts_as_dense_zero_not_crash():
    """兩個 judge 都 None → both_dense_zero（None or 0 < 0.30），不進 format 路徑。"""
    rows = [_row(j1_conf=None, j3_conf=None)]
    result = analyze(rows)
    assert result["both_dense_zero_count"] == 1


def test_normal_float_confidence_still_formats_two_decimals():
    """回歸：正常 float 信心仍格式化兩位小數（沒被 safe formatter 改壞）。"""
    rows = [_row(j1_conf=0.55, j3_conf=0.91, j1_name="music", j3_name="skip")]
    result = analyze(rows)
    disagree = result["j1_j3_disagree"]
    assert "music(0.55)" in disagree[0]["j1"]
    assert "skip(0.91)" in disagree[0]["j3"]
