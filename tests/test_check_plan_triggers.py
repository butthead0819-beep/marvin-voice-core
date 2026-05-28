"""Plan trigger condition checker — 確認 Plan 4 (Intent Gap A.5 Clustering)
trigger 只計 non-UNKNOWN gap，因為 UNKNOWN 是「無意圖雜訊」，clustering 對它
無事可做（contract 見 intent_gap.py 的 IntentGapRecord docstring）。

Plan 7 (MemoryCallback embedding) trigger 讀 speak_outcomes.jsonl
winner=="MemoryCallbackAgent" 計數；觀察期 <14 天視為觀察不足、不觸發。
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import scripts.check_plan_triggers as cpt


def _write_gaps(path: pathlib.Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_intent_gap_clustering_excludes_unknown(monkeypatch, tmp_path):
    gaps = tmp_path / "agent_gaps.jsonl"
    _write_gaps(
        gaps,
        [
            {"intent_type": "UNKNOWN", "raw_query": "雜訊1"},
            {"intent_type": "UNKNOWN", "raw_query": "雜訊2"},
            {"intent_type": "UNKNOWN", "raw_query": "雜訊3"},
            {"intent_type": "UNKNOWN", "raw_query": "雜訊4"},
            {"intent_type": "UNKNOWN", "raw_query": "雜訊5"},
        ],
    )
    monkeypatch.setattr(cpt, "RECORDS", tmp_path)

    result = cpt.check_intent_gap_clustering()

    assert result["met"] is False
    assert "0" in result["current"]


def test_intent_gap_clustering_counts_only_non_unknown(monkeypatch, tmp_path):
    gaps = tmp_path / "agent_gaps.jsonl"
    _write_gaps(
        gaps,
        [
            {"intent_type": "UNKNOWN"},
            {"intent_type": "replay_user_history"},
            {"intent_type": "UNKNOWN"},
            {"intent_type": "play_user_past_songs"},
            {"intent_type": "change_voice"},
            {"intent_type": "show_lyrics"},
            {"intent_type": "UNKNOWN"},
            {"intent_type": "skip_current_track"},
        ],
    )
    monkeypatch.setattr(cpt, "RECORDS", tmp_path)

    result = cpt.check_intent_gap_clustering()

    assert result["met"] is True
    assert "5" in result["current"]


def test_intent_gap_clustering_below_threshold(monkeypatch, tmp_path):
    gaps = tmp_path / "agent_gaps.jsonl"
    _write_gaps(
        gaps,
        [
            {"intent_type": "UNKNOWN"},
            {"intent_type": "replay_user_history"},
            {"intent_type": "UNKNOWN"},
            {"intent_type": "change_voice"},
        ],
    )
    monkeypatch.setattr(cpt, "RECORDS", tmp_path)

    result = cpt.check_intent_gap_clustering()

    assert result["met"] is False
    assert "2" in result["current"]


def test_intent_gap_clustering_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(cpt, "RECORDS", tmp_path)

    result = cpt.check_intent_gap_clustering()

    assert result["met"] is False
    assert "0" in result["current"]


def test_intent_gap_clustering_skips_malformed_lines(monkeypatch, tmp_path):
    gaps = tmp_path / "agent_gaps.jsonl"
    gaps.parent.mkdir(parents=True, exist_ok=True)
    with gaps.open("w", encoding="utf-8") as f:
        f.write('{"intent_type": "replay_user_history"}\n')
        f.write("not-json-line\n")
        f.write('{"intent_type": "UNKNOWN"}\n')
        f.write('{"intent_type": "show_lyrics"}\n')
    monkeypatch.setattr(cpt, "RECORDS", tmp_path)

    result = cpt.check_intent_gap_clustering()

    assert result["met"] is False
    assert "2" in result["current"]


# ─── Plan 7: MemoryCallback embedding 觸發測試 ─────────────────────────────────


def _write_outcomes(path: pathlib.Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _ts_days_ago(n: float) -> float:
    return time.time() - n * 86400


def test_memory_callback_embedding_observation_insufficient(monkeypatch, tmp_path):
    """觀察期 <14 天 → 不觸發（不管 callback 多少筆）。"""
    outcomes = tmp_path / "speak_outcomes.jsonl"
    _write_outcomes(
        outcomes,
        [
            {"ts": _ts_days_ago(0), "winner": "ProactiveTopicAgent"},
            {"ts": _ts_days_ago(1), "winner": "MemoryCallbackAgent"},
            {"ts": _ts_days_ago(2), "winner": "MemoryCallbackAgent"},
        ],
    )
    monkeypatch.setattr(cpt, "RECORDS", tmp_path)

    result = cpt.check_memory_callback_embedding()

    assert result["met"] is False
    assert "觀察" in result["current"]


def test_memory_callback_embedding_below_one_per_day(monkeypatch, tmp_path):
    """觀察 14 天 + callback win/天 <1 → 觸發（建議升 embedding）。"""
    outcomes = tmp_path / "speak_outcomes.jsonl"
    records = []
    for i in range(14):
        records.append({"ts": _ts_days_ago(i), "winner": "ProactiveTopicAgent"})
    # 14 天總共 3 筆 callback → rate 0.21/天 <1
    records.append({"ts": _ts_days_ago(1), "winner": "MemoryCallbackAgent"})
    records.append({"ts": _ts_days_ago(5), "winner": "MemoryCallbackAgent"})
    records.append({"ts": _ts_days_ago(10), "winner": "MemoryCallbackAgent"})
    _write_outcomes(outcomes, records)
    monkeypatch.setattr(cpt, "RECORDS", tmp_path)

    result = cpt.check_memory_callback_embedding()

    assert result["met"] is True
    assert "3" in result["current"]


def test_memory_callback_embedding_above_one_per_day(monkeypatch, tmp_path):
    """觀察 14 天 + callback win/天 ≥1 → 不觸發（char-overlap 已夠用）。"""
    outcomes = tmp_path / "speak_outcomes.jsonl"
    records = []
    for i in range(14):
        records.append({"ts": _ts_days_ago(i), "winner": "ProactiveTopicAgent"})
        records.append({"ts": _ts_days_ago(i), "winner": "MemoryCallbackAgent"})
        records.append({"ts": _ts_days_ago(i), "winner": "MemoryCallbackAgent"})
    _write_outcomes(outcomes, records)
    monkeypatch.setattr(cpt, "RECORDS", tmp_path)

    result = cpt.check_memory_callback_embedding()

    assert result["met"] is False


def test_memory_callback_embedding_only_counts_window(monkeypatch, tmp_path):
    """14 天窗口外的舊資料不計入觀察天數也不計入 callback count。"""
    outcomes = tmp_path / "speak_outcomes.jsonl"
    records = []
    for i in range(14):
        records.append({"ts": _ts_days_ago(i), "winner": "ProactiveTopicAgent"})
    records.append({"ts": _ts_days_ago(30), "winner": "MemoryCallbackAgent"})
    records.append({"ts": _ts_days_ago(60), "winner": "MemoryCallbackAgent"})
    _write_outcomes(outcomes, records)
    monkeypatch.setattr(cpt, "RECORDS", tmp_path)

    result = cpt.check_memory_callback_embedding()

    assert result["met"] is True
    assert "0" in result["current"]


def test_memory_callback_embedding_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(cpt, "RECORDS", tmp_path)

    result = cpt.check_memory_callback_embedding()

    assert result["met"] is False
    assert "speak_outcomes" in result["current"] or "0" in result["current"]


def test_memory_callback_embedding_skips_malformed_lines(monkeypatch, tmp_path):
    outcomes = tmp_path / "speak_outcomes.jsonl"
    outcomes.parent.mkdir(parents=True, exist_ok=True)
    with outcomes.open("w", encoding="utf-8") as f:
        for i in range(14):
            f.write(json.dumps({"ts": _ts_days_ago(i), "winner": "ProactiveTopicAgent"}) + "\n")
        f.write("not-json-line\n")
        f.write(json.dumps({"ts": _ts_days_ago(2), "winner": "MemoryCallbackAgent"}) + "\n")
    monkeypatch.setattr(cpt, "RECORDS", tmp_path)

    result = cpt.check_memory_callback_embedding()

    assert result["met"] is True  # 1/14 <1
