"""analyze_agent_gaps — agent_gaps.jsonl dedup-aware 計數（Plan 4 daily ritual）。

核心修正（2026-05-30 buy_milk/replay_user_history 假觸發）：
occurrence count 必須按 **distinct (speaker, raw_query)** 算，不是 raw line count。
同一句重複 N 次（QA 連發 / 結巴 / 跳針）只能算 1 次 occurrence，否則門檻形同虛設。

threshold=2（feedback_intent_gap_threshold.md，使用者拍板激進補 agent）。
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.analyze_agent_gaps import analyze, load


def _gap(intent_type="replay_user_history", speaker="Alice", raw="我剛才說了什麼"):
    return {
        "utterance_id": "u", "ts": 0.0, "speaker": speaker, "mode": "normal",
        "raw_query": raw, "cleaned_query": raw, "intent_type": intent_type,
        "slots": {}, "nearest_agent": None, "nearest_distance": 1.0,
        "ack_text": "", "acknowledged": False, "schema_version": 1,
    }


# ── load ──────────────────────────────────────────────────────────────────────

def test_load_skips_blank_lines(tmp_path: Path):
    p = tmp_path / "g.jsonl"
    p.write_text(json.dumps(_gap()) + "\n\n  \n" + json.dumps(_gap()) + "\n", encoding="utf-8")
    assert len(load(p)) == 2


# ── dedup 是核心 ───────────────────────────────────────────────────────────────

def test_identical_repeats_count_as_one_distinct_occurrence():
    """7 筆同 (speaker, raw_query) → distinct_count=1（raw_count=7 仍保留供觀察）。
    這正是 buy_milk/replay 假觸發的修正點。"""
    rows = [_gap(intent_type="replay_user_history") for _ in range(7)]
    result = analyze(rows)
    rep = next(i for i in result["intents"] if i["intent_type"] == "replay_user_history")
    assert rep["raw_count"] == 7
    assert rep["distinct_count"] == 1
    assert rep["ready_to_implement"] is False  # 1 < 2 → 不該觸發


def test_distinct_phrasings_accumulate_toward_threshold():
    """同 intent_type 但不同講法 → distinct 累加，≥2 才 ready（真有機多樣性）。"""
    rows = [
        _gap(intent_type="set_alarm", raw="幫我設個鬧鐘"),
        _gap(intent_type="set_alarm", raw="提醒我八點起床"),
    ]
    result = analyze(rows)
    alarm = next(i for i in result["intents"] if i["intent_type"] == "set_alarm")
    assert alarm["distinct_count"] == 2
    assert alarm["ready_to_implement"] is True


def test_same_phrase_different_speakers_counts_as_two():
    """不同 speaker 講同句 → 2 個 distinct occurrence（跨人＝有機需求訊號）。"""
    rows = [
        _gap(intent_type="set_alarm", speaker="Alice", raw="設鬧鐘"),
        _gap(intent_type="set_alarm", speaker="Bob", raw="設鬧鐘"),
    ]
    result = analyze(rows)
    alarm = next(i for i in result["intents"] if i["intent_type"] == "set_alarm")
    assert alarm["distinct_count"] == 2
    assert alarm["ready_to_implement"] is True


# ── UNKNOWN 排除 ──────────────────────────────────────────────────────────────

def test_unknown_excluded_from_intents():
    """UNKNOWN 是無意圖雜訊，不參與 ready 計算（對齊 feedback_trigger_excludes_sentinels）。"""
    rows = [_gap(intent_type="UNKNOWN", raw=f"噪音{i}") for i in range(10)]
    result = analyze(rows)
    assert result["total"] == 10
    assert result["total_non_unknown"] == 0
    assert result["intents"] == []
    assert result["ready_count"] == 0


# ── 排序 + 摘要 ───────────────────────────────────────────────────────────────

def test_intents_sorted_by_distinct_count_desc():
    rows = (
        [_gap(intent_type="a", raw=f"x{i}") for i in range(3)]
        + [_gap(intent_type="b", raw="y")]
    )
    result = analyze(rows)
    assert [i["intent_type"] for i in result["intents"]] == ["a", "b"]


def test_ready_count_reflects_only_distinct_threshold():
    """模擬真實污染場景：buy_milk×7 + replay×7（都 distinct=1）→ ready_count=0。"""
    rows = (
        [_gap(intent_type="buy_milk", raw="記一下要買牛奶") for _ in range(7)]
        + [_gap(intent_type="replay_user_history", raw="我剛才說了什麼") for _ in range(7)]
    )
    result = analyze(rows)
    assert result["total_non_unknown"] == 14
    assert result["ready_count"] == 0  # 假觸發消失：兩個都 distinct=1


def test_analyze_empty():
    result = analyze([])
    assert result["total"] == 0
    assert result["intents"] == []


# ── clusterable_gaps（2026-06-12）───────────────────────────────────────────
# Bug：resolved 過濾原本只在 save_clusters 比對 LLM 自創的 cluster_id / raw-query
# members，永遠對不上 resolved 的 intent_type（minecraft_query ≠ game_knowledge_query
# 誤報 ready）。修法：送 LLM 前就按 gap record 的 intent_type 濾掉 resolved，
# 觸發門檻 ≥5 也只數 clusterable（per feedback_trigger_excludes_sentinels：
# 已被 agent 服務的訊號不該灌門檻）。

def test_clusterable_gaps_excludes_unknown_and_resolved():
    from scripts.analyze_agent_gaps import clusterable_gaps

    gaps = [
        _gap("UNKNOWN"),
        _gap("strong_play", raw="馬文波馬文播放"),
        _gap("game_knowledge_query", raw="查麥塊鑽石"),
        _gap("game_knowledge_query", raw="查麥塊鐵巨人"),
        _gap("volume_down", raw="小聲一點"),
    ]
    out = clusterable_gaps(gaps, resolved={"game_knowledge_query"})

    assert [r["intent_type"] for r in out] == ["strong_play", "volume_down"]


def test_clusterable_gaps_no_resolved_passes_all_non_unknown():
    from scripts.analyze_agent_gaps import clusterable_gaps

    gaps = [_gap("UNKNOWN"), _gap("a"), _gap("b")]
    out = clusterable_gaps(gaps, resolved=set())

    assert [r["intent_type"] for r in out] == ["a", "b"]


def test_should_cluster_counts_only_clusterable():
    from scripts.analyze_agent_gaps import should_cluster

    # 6 筆 non-UNKNOWN 但 3 筆已 resolved → 3 < 5 不觸發
    gaps = (
        [_gap("game_knowledge_query", raw=f"q{i}") for i in range(3)]
        + [_gap("strong_play"), _gap("playback_control"), _gap("volume_down")]
    )
    assert should_cluster(gaps, resolved={"game_knowledge_query"}) is False
    assert should_cluster(gaps, resolved=set()) is True


# ── save_clusters ──────────────────────────────────────────────────────────────

def test_save_clusters_filters_resolved_and_assigns_status(tmp_path: Path):
    from scripts.analyze_agent_gaps import save_clusters
    
    clusters = [
        {"cluster_id": "buy_milk", "members": ["buy_milk_1"], "occurrence_count": 2},
        {"cluster_id": "resolved_intent", "members": ["resolved_1"], "occurrence_count": 3},
        {"cluster_id": "monitoring_intent", "members": ["monitoring_1"], "occurrence_count": 1},
    ]
    
    resolved = {"resolved_intent"}
    output = tmp_path / "clusters.json"
    
    save_clusters(clusters, resolved, output)
    
    assert output.exists()
    data = json.loads(output.read_text(encoding="utf-8"))
    
    # resolved_intent 應該被過濾掉
    assert len(data) == 2
    
    # 檢查 buy_milk
    buy_milk = next(c for c in data if c["cluster_id"] == "buy_milk")
    assert buy_milk["status"] == "ready_to_implement"
    assert buy_milk["occurrence_count"] == 2
    
    # 檢查 monitoring_intent
    monitoring = next(c for c in data if c["cluster_id"] == "monitoring_intent")
    assert monitoring["status"] == "monitoring"
    assert monitoring["occurrence_count"] == 1

