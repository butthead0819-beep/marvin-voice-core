"""analyze_judge_outcomes.analyze() 回歸測試。

涵蓋兩波修正：
1. (2026-05-30) confidence=None → f"{conf:.2f}" 炸 TypeError，daily ritual 分析 crash。
   修：safe formatter，None → "?"（不假裝 0.00，會混淆真實 0 信心）。
2. (2026-06-03) 假性災難一致率：腳本把 cancelled 的 J1 當「沒出價」、拿 bid_name
   字串硬比（guard(0.96) vs cleaner_judge(0.00) 都是 NO_INTENT 卻判不一致），
   吐出 49.5% 假數字（真實 96.4%）。修：_outcome 語意分桶 + 只配對兩邊 completed。
"""
from __future__ import annotations

from scripts.analyze_judge_outcomes import _outcome, analyze


def _judge(name, *, status="completed", conf=None, bid=None, lat=1.0):
    return {"name": name, "status": status, "confidence": conf,
            "bid_name": bid, "bid_reason": None, "latency_ms": lat}


def _row(j1_conf, j3_conf, *, j1_name="music", j3_name="guard", raw="播歌",
         j1_status="completed", j3_status="completed",
         winning_judge="j1_regex"):
    return {
        "raw_query": raw,
        "winning_judge": winning_judge,
        "winner_name": j1_name,
        "winner_confidence": j1_conf if j1_conf is not None else 0,
        "judges": [
            {"name": "j1_regex", "bid_name": j1_name, "confidence": j1_conf,
             "latency_ms": 1.0, "bid_reason": "r1", "status": j1_status},
            {"name": "j3_cleaner_precomputed", "bid_name": j3_name, "confidence": j3_conf,
             "latency_ms": 5.0, "bid_reason": "r3", "status": j3_status},
        ],
    }


# ── 波 1：None-confidence 不 crash ────────────────────────────────────────────

def test_analyze_does_not_crash_when_one_judge_confidence_is_none():
    rows = [_row(j1_conf=0.5, j3_conf=None)]
    result = analyze(rows)  # 不該拋 TypeError
    assert result["total"] == 1


def test_disagree_entry_renders_none_confidence_as_placeholder():
    """None 信心顯示成 '?' 不是 0.00。j1 music(0.5) vs j3 guard(None) → 語意不一致。"""
    rows = [_row(j1_conf=0.5, j3_conf=None, j1_name="music", j3_name="guard")]
    result = analyze(rows)
    disagree = result["semantic_disagree"]
    assert len(disagree) == 1
    assert "guard(?)" in disagree[0]["j3"]
    assert "music(0.50)" in disagree[0]["j1"]


def test_normal_float_confidence_still_formats_two_decimals():
    rows = [_row(j1_conf=0.55, j3_conf=0.91, j1_name="music", j3_name="skip")]
    result = analyze(rows)
    disagree = result["semantic_disagree"]
    assert "music(0.55)" in disagree[0]["j1"]
    assert "skip(0.91)" in disagree[0]["j3"]


# ── 波 2：_outcome 語意分桶 ───────────────────────────────────────────────────

def test_outcome_cancelled_returns_none():
    assert _outcome(_judge("j1_regex", status="cancelled")) is None


def test_outcome_guard_is_no_intent():
    assert _outcome(_judge("j1_regex", conf=0.96, bid="guard")) == "NO_INTENT"


def test_outcome_cleaner_judge_dense_zero_is_no_intent():
    assert _outcome(_judge("j3_cleaner_precomputed", conf=0.0, bid="cleaner_judge")) == "NO_INTENT"


def test_outcome_low_confidence_is_no_intent():
    assert _outcome(_judge("j3_cleaner_precomputed", conf=0.1, bid="music")) == "NO_INTENT"


def test_outcome_actionable_agent_returns_name():
    assert _outcome(_judge("j3_cleaner_precomputed", conf=0.95, bid="music")) == "music"


# ── 波 2：analyze 一致率核心修正 ──────────────────────────────────────────────

def test_guard_and_cleaner_dense_zero_count_as_agree():
    """兩邊都 NO_INTENT（不同 bid_name）→ 語意一致，非 disagree。"""
    rows = [_row(j1_conf=0.96, j3_conf=0.0, j1_name="guard", j3_name="cleaner_judge",
                 raw="阿文法文播放")]
    r = analyze(rows)
    assert r["semantic_agree_rate"] == 1.0
    assert r["completed_pairs"] == 1
    assert r["both_no_intent_count"] == 1


def test_cancelled_j1_excluded_from_pairs():
    """J1 被 precomputed J3 取消 → 不納入配對（不是 disagree）。"""
    rows = [_row(j1_conf=None, j3_conf=0.95, j1_name="music", j3_name="music",
                 raw="播放林俊傑的江南", j1_status="cancelled",
                 winning_judge="j3_cleaner_precomputed")]
    r = analyze(rows)
    assert r["completed_pairs"] == 0
    assert r["j1_cancelled_count"] == 1
    assert r["semantic_disagree"] == []


def test_guard_too_aggressive_surfaced():
    """J1 NO_INTENT 但 J3 救回真 intent → guard_too_aggressive。"""
    rows = [_row(j1_conf=0.96, j3_conf=0.80, j1_name="guard", j3_name="music",
                 raw="播放孤勇的")]
    r = analyze(rows)
    assert r["semantic_agree_rate"] == 0.0
    assert len(r["guard_too_aggressive"]) == 1
    assert r["guard_too_aggressive"][0]["raw"] == "播放孤勇的"


def test_j1_false_positive_surfaced():
    """J1 有 intent、J3 cleaned 後判無 → J1 over-trigger。"""
    rows = [_row(j1_conf=0.85, j3_conf=0.0, j1_name="music", j3_name="cleaner_judge",
                 raw="始作俑者")]
    r = analyze(rows)
    assert len(r["j1_false_positive"]) == 1
    assert r["j1_false_positive"][0]["raw"] == "始作俑者"


def test_fastpath_counts_j1_win():
    rows = [_row(j1_conf=0.85, j3_conf=None, j1_name="music",
                 j3_status="cancelled", winning_judge="j1_regex")]
    r = analyze(rows)
    assert r["j1_fastpath_rate"] == 1.0


def test_j2_executed_detected_via_j1_reason_footprint():
    """J2 是 J1 外的 veto wrapper（非獨立 judge）→ 靠 J1 bid_reason 足跡觀測。"""
    def _j1(reason):
        j = _judge("j1_regex", conf=0.95, bid="music")
        j["bid_reason"] = reason
        return j
    rows = [
        # ran 沒否決
        {"raw_query": "a", "winning_judge": "j1_regex", "winner_name": "music",
         "judges": [_j1("weak_play_specific|j2_ran(chat=False,0.10):no_reason"),
                    _judge("j3_cleaner_precomputed", status="cancelled")]},
        # 否決
        {"raw_query": "b", "winning_judge": "j3_cleaner_precomputed", "winner_name": "music",
         "judges": [_j1("vetoed_by_chat(0.90):modal|orig:weak_play"),
                    _judge("j3_cleaner_precomputed", conf=0.95, bid="music")]},
        # fail-safe（timeout）
        {"raw_query": "c", "winning_judge": "j1_regex", "winner_name": "music",
         "judges": [_j1("weak_play|j2_ran(chat=False,0.00):llm_timeout"),
                    _judge("j3_cleaner_precomputed", status="cancelled")]},
        # 沒跑 J2（short-circuit，無足跡）
        {"raw_query": "d", "winning_judge": "j1_regex", "winner_name": "music",
         "judges": [_j1("weak_play_specific"),
                    _judge("j3_cleaner_precomputed", status="cancelled")]},
    ]
    r = analyze(rows)
    assert r["j2_executed_count"] == 3
    assert r["j2_veto_count"] == 1
    assert r["j2_failsafe_count"] == 1
