"""Telemetry writer — serialize RaceResult to jsonl row, append to records/.

J1 hit rate / 各 judge latency / fast-path vs fallback 比率，全靠這個 log。
schema 穩定後外部分析可直接 jq / pandas 吃，所以序列化欄位是契約。
"""
from __future__ import annotations

import json

import pytest

from intent_bus import Bid, IntentContext
from intent_judges.race import JudgeOutcome, RaceResult
from intent_judges.telemetry import serialize_outcome, write_race_outcome


async def _async_noop() -> None:
    pass


def _bid(name: str, conf: float, reason: str = "ok") -> Bid:
    return Bid(name=name, confidence=conf, handler=_async_noop, reason=reason)


def _ctx(query: str = "打開 YouTube") -> IntentContext:
    return IntentContext(
        speaker="alice", raw_text=query, query=query, original_raw=query,
        wake_intent=0.9, stream_active=False, game_mode=False, is_owner=False,
        now=0.0, mode="normal",
    )


def _result(winner: Bid, winning_judge: str | None,
            outcomes: list[JudgeOutcome], total_ms: float = 100.0) -> RaceResult:
    return RaceResult(winner=winner, winning_judge=winning_judge,
                      outcomes=outcomes, total_ms=total_ms)


# ── serialize_outcome shape ───────────────────────────────────────────────


def test_serialize_outcome_contains_utterance_and_speaker():
    result = _result(_bid("music", 0.95), "J1",
                     [JudgeOutcome("J1", "completed", _bid("music", 0.95), 4.2)])
    row = serialize_outcome("utt-123", _ctx("打開 YouTube"), result)
    assert row["utterance_id"] == "utt-123"
    assert row["speaker"] == "alice"
    assert row["mode"] == "normal"
    assert row["raw_query"] == "打開 YouTube"


def test_serialize_outcome_contains_winner_metadata():
    result = _result(_bid("music", 0.95, reason="strong_play:打開"), "J1",
                     [JudgeOutcome("J1", "completed", _bid("music", 0.95), 4.2)],
                     total_ms=4.5)
    row = serialize_outcome("utt-1", _ctx(), result)
    assert row["winning_judge"] == "J1"
    assert row["winner_name"] == "music"
    assert row["winner_confidence"] == 0.95
    assert row["winner_reason"] == "strong_play:打開"
    assert row["total_ms"] == 4.5


def test_serialize_outcome_includes_one_record_per_judge():
    outcomes = [
        JudgeOutcome("J1", "completed", _bid("music", 0.95), 4.0),
        JudgeOutcome("J2", "cancelled", None, 30.0),
        JudgeOutcome("J3", "cancelled", None, 30.0),
    ]
    row = serialize_outcome("utt-1", _ctx(), _result(_bid("music", 0.95), "J1", outcomes))
    assert len(row["judges"]) == 3
    assert {j["name"] for j in row["judges"]} == {"J1", "J2", "J3"}


def test_serialize_outcome_completed_judge_carries_bid_metadata():
    outcomes = [JudgeOutcome("J1", "completed", _bid("music", 0.85, reason="hit"), 4.0)]
    row = serialize_outcome("utt-1", _ctx(), _result(_bid("music", 0.85), "J1", outcomes))
    j = row["judges"][0]
    assert j["status"] == "completed"
    assert j["latency_ms"] == 4.0
    assert j["confidence"] == 0.85
    assert j["bid_name"] == "music"
    assert j["bid_reason"] == "hit"
    assert j["error"] is None


def test_serialize_outcome_cancelled_judge_has_no_bid():
    outcomes = [JudgeOutcome("J2", "cancelled", None, 12.5)]
    row = serialize_outcome("utt-1", _ctx(), _result(_bid("J1_winner", 0.95), "J1", outcomes))
    j = row["judges"][0]
    assert j["status"] == "cancelled"
    assert j["confidence"] is None
    assert j["bid_name"] is None
    assert j["bid_reason"] is None
    assert j["latency_ms"] == 12.5


def test_serialize_outcome_exception_judge_includes_error_class():
    outcomes = [JudgeOutcome("J2", "exception", None, 8.0, error="TimeoutError")]
    row = serialize_outcome("utt-1", _ctx(), _result(_bid("J1", 0.95), "J1", outcomes))
    j = row["judges"][0]
    assert j["status"] == "exception"
    assert j["error"] == "TimeoutError"
    assert j["confidence"] is None


def test_serialize_outcome_dense_zero_winner_has_null_winning_judge():
    """no_judges / timeout → winning_judge None，但 winner Bid 仍存在。"""
    result = _result(_bid("judges_race", 0.0, reason="no_judges"), None, [])
    row = serialize_outcome("utt-1", _ctx(""), result)
    assert row["winning_judge"] is None
    assert row["winner_confidence"] == 0.0


# ── write_race_outcome (file I/O) ────────────────────────────────────────


def test_write_race_outcome_appends_single_line(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    outcomes = [JudgeOutcome("J1", "completed", _bid("music", 0.95), 4.0)]
    write_race_outcome(path, "utt-1", _ctx(), _result(_bid("music", 0.95), "J1", outcomes))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["utterance_id"] == "utt-1"


def test_write_race_outcome_appends_across_multiple_calls(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    outcomes = [JudgeOutcome("J1", "completed", _bid("music", 0.95), 4.0)]
    write_race_outcome(path, "utt-1", _ctx(), _result(_bid("music", 0.95), "J1", outcomes))
    write_race_outcome(path, "utt-2", _ctx(), _result(_bid("music", 0.95), "J1", outcomes))
    write_race_outcome(path, "utt-3", _ctx(), _result(_bid("music", 0.95), "J1", outcomes))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    ids = [json.loads(l)["utterance_id"] for l in lines]
    assert ids == ["utt-1", "utt-2", "utt-3"]


def test_write_race_outcome_creates_parent_directory(tmp_path):
    path = tmp_path / "nested" / "deep" / "outcomes.jsonl"
    outcomes = [JudgeOutcome("J1", "completed", _bid("music", 0.95), 4.0)]
    write_race_outcome(path, "utt-1", _ctx(), _result(_bid("music", 0.95), "J1", outcomes))
    assert path.exists()


def test_write_race_outcome_preserves_chinese_text(tmp_path):
    """ensure_ascii=False，繁中要原樣寫入而不是 \\u 跳脫。"""
    path = tmp_path / "outcomes.jsonl"
    outcomes = [JudgeOutcome("J1", "completed",
                             _bid("music", 0.95, reason="打開 YouTube"), 4.0)]
    write_race_outcome(path, "utt-1", _ctx("打開 YouTube"),
                       _result(_bid("music", 0.95), "J1", outcomes))
    raw = path.read_text(encoding="utf-8")
    assert "打開 YouTube" in raw  # 不該被 escape 成 打開


def test_write_race_outcome_produces_valid_jsonl(tmp_path):
    """每行都該是合法 JSON，外部 jq/pandas 可直接吃。"""
    path = tmp_path / "outcomes.jsonl"
    for i in range(5):
        outcomes = [JudgeOutcome("J1", "completed", _bid("music", 0.95), 4.0)]
        write_race_outcome(path, f"utt-{i}", _ctx(),
                          _result(_bid("music", 0.95), "J1", outcomes))
    for line in path.read_text(encoding="utf-8").splitlines():
        json.loads(line)  # 不能 raise
