"""TDD: SpeakOutcome — proactive 發話結果 log，為「求生 / 利基特化」鋪資料。

schema_version 從第 1 個 record 算起（per 2026-05-25 design discipline）。
紀錄：誰贏、信心、原因、tick trigger、N 秒內有沒有 STT 回聲（弱 quality signal）。

mirror records/agent_recommendations.jsonl 的 append-only / never-raise 慣例。
"""
from __future__ import annotations

import json
from pathlib import Path

from speak_outcome import SpeakOutcome, append_speak_outcome, read_speak_outcomes


def _sample(**overrides) -> SpeakOutcome:
    base = dict(
        ts=1779700000.0,
        trigger="idle_tick",
        winner="DuckingAgent",
        confidence=0.85,
        reason="turn_taking_collision",
        bid_count=3,
        had_followup_stt=True,
        silence_seconds=4.5,
        present_speakers=("大肚", "狗與露"),
    )
    base.update(overrides)
    return SpeakOutcome(**base)


# ── schema_version 紀律 ───────────────────────────────────────────────────────

def test_outcome_has_schema_version_field():
    """每個 record 都帶 schema_version，第一版 = 1。"""
    rec = _sample()
    assert rec.schema_version == 1


def test_jsonline_includes_schema_version():
    """serialize 後 schema_version 必須在 JSON 內，外部 consumer 才能版本判斷。"""
    rec = _sample()
    data = json.loads(rec.to_jsonline())
    assert data["schema_version"] == 1


# ── roundtrip ────────────────────────────────────────────────────────────────

def test_append_then_read_roundtrip(tmp_path):
    log = tmp_path / "outcomes.jsonl"
    rec = _sample()
    append_speak_outcome(rec, path=log)
    got = list(read_speak_outcomes(path=log))
    assert len(got) == 1
    assert got[0].winner == "DuckingAgent"
    assert got[0].had_followup_stt is True
    assert got[0].present_speakers == ("大肚", "狗與露")


def test_multiple_appends_preserve_order(tmp_path):
    log = tmp_path / "outcomes.jsonl"
    for i in range(3):
        append_speak_outcome(_sample(ts=1000.0 + i, winner=f"Agent{i}"), path=log)
    got = list(read_speak_outcomes(path=log))
    assert [r.winner for r in got] == ["Agent0", "Agent1", "Agent2"]


# ── never raises ──────────────────────────────────────────────────────────────

def test_append_to_unwritable_path_does_not_raise(tmp_path):
    """IO 失敗永不傳染——caller 在 hot path。"""
    bad_path = tmp_path / "nonexistent_dir" / "outcomes.jsonl"
    bad_path.parent.chmod(0o000) if bad_path.parent.exists() else None
    # 直接給一個一定寫不進去的路徑（父目錄都沒）— 不可 raise
    append_speak_outcome(_sample(), path=Path("/nonexistent_root_dir_xyz/outcomes.jsonl"))


def test_read_skips_corrupt_lines(tmp_path):
    log = tmp_path / "outcomes.jsonl"
    append_speak_outcome(_sample(winner="Good1"), path=log)
    with log.open("a", encoding="utf-8") as f:
        f.write("not json garbage\n")
    append_speak_outcome(_sample(winner="Good2"), path=log)
    got = list(read_speak_outcomes(path=log))
    assert [r.winner for r in got] == ["Good1", "Good2"]


def test_read_missing_file_returns_empty(tmp_path):
    got = list(read_speak_outcomes(path=tmp_path / "nope.jsonl"))
    assert got == []
