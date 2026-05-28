"""Plan trigger condition checker — 確認 Plan 4 (Intent Gap A.5 Clustering)
trigger 只計 non-UNKNOWN gap，因為 UNKNOWN 是「無意圖雜訊」，clustering 對它
無事可做（contract 見 intent_gap.py 的 IntentGapRecord docstring）。
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

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
