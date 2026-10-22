"""turn_outcome.py 單元測試（意圖漏斗步驟 1）。"""
from __future__ import annotations

import asyncio
import json

import pytest

import turn_outcome


def _read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_turn_key_formats_speaker_and_ts():
    assert turn_outcome.turn_key("A", 12.3456) == "A@12.346"


@pytest.mark.parametrize(
    "was_fast, accepted, hits, override, bypass, expected",
    [
        (True, True, ["guard_echo"], "window_session", None, "window_session"),
        (False, True, [], None, None, "wake_word"),
        (False, True, [], None, "bypass_strong_voice", "bypass_strong_voice"),
        (False, False, ["guard_echo", "guard_global"], None, None, "guard_echo"),
        (True, False, [], None, None, "guard_double_wake"),
        (False, False, [], None, None, None),
    ],
)
def test_gate_reason_table(was_fast, accepted, hits, override, bypass, expected):
    assert turn_outcome.gate_reason(was_fast, accepted, hits, override, bypass) == expected


def test_write_gate_decision_accept_block_and_window(tmp_path, monkeypatch):
    path = tmp_path / "gate.jsonl"
    monkeypatch.setattr(turn_outcome, "GATE_PATH", path)

    turn_outcome.write_gate_decision(
        speaker="Alice", ts=1.0, track="A", reason="wake_word", iba_confidence=0.9)
    turn_outcome.write_gate_decision(
        speaker="Bob", ts=2.0, track="A", reason="guard_echo", iba_confidence=0.1)
    turn_outcome.write_gate_decision(
        speaker="Carl", ts=3.0, track="A", reason="window_session", iba_confidence=None)

    recs = _read_jsonl(path)
    assert len(recs) == 3
    assert recs[0]["decision"] == "accept"
    assert recs[1]["decision"] == "block"
    assert recs[2]["window"] == "session"
    assert set(recs[0].keys()) == {
        "ts", "turn_key", "track", "speaker", "decision", "reason", "window", "iba_confidence",
    }


def test_write_gate_decision_none_reason_writes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "gate.jsonl"
    monkeypatch.setattr(turn_outcome, "GATE_PATH", path)
    turn_outcome.write_gate_decision(
        speaker="Alice", ts=1.0, track="A", reason=None, iba_confidence=0.9)
    assert not path.exists()


@pytest.mark.asyncio
async def test_run_turn_success_writes_one_record_with_expected_keys(tmp_path, monkeypatch):
    path = tmp_path / "out.jsonl"
    monkeypatch.setattr(turn_outcome, "OUTCOMES_PATH", path)

    async def body():
        turn_outcome.mark_route("legacy:status_keywords")

    await turn_outcome.run_turn(body(), speaker="Alice", ts=1.0, discovered_by="wake_word")

    recs = _read_jsonl(path)
    assert len(recs) == 1
    assert set(recs[0].keys()) == {
        "turn_id", "ts", "speaker", "discovered_by", "route", "intent",
        "slots", "executed", "corrected", "satisfaction_signal",
    }
    assert recs[0]["route"] == "legacy:status_keywords"
    assert recs[0]["executed"] is True


@pytest.mark.asyncio
async def test_mark_route_first_wins(tmp_path, monkeypatch):
    path = tmp_path / "out.jsonl"
    monkeypatch.setattr(turn_outcome, "OUTCOMES_PATH", path)

    async def body():
        turn_outcome.mark_route("first")
        turn_outcome.mark_route("second")

    await turn_outcome.run_turn(body(), speaker="A", ts=1.0, discovered_by="wake_word")
    recs = _read_jsonl(path)
    assert recs[0]["route"] == "first"


@pytest.mark.asyncio
async def test_note_winner_intent_takes_last_route_takes_first(tmp_path, monkeypatch):
    path = tmp_path / "out.jsonl"
    monkeypatch.setattr(turn_outcome, "OUTCOMES_PATH", path)

    class FakeBid:
        def __init__(self, name, missing_slots=None):
            self.name = name
            self.missing_slots = missing_slots or []

    async def body():
        turn_outcome.note_winner(FakeBid("music", ["song_title"]))
        turn_outcome.note_winner(FakeBid("replay"))

    await turn_outcome.run_turn(body(), speaker="A", ts=1.0, discovered_by="wake_word")
    recs = _read_jsonl(path)
    assert recs[0]["intent"] == "replay"
    assert recs[0]["route"] == "bus:music"


@pytest.mark.asyncio
async def test_no_route_marked_results_in_silent_and_not_executed(tmp_path, monkeypatch):
    path = tmp_path / "out.jsonl"
    monkeypatch.setattr(turn_outcome, "OUTCOMES_PATH", path)

    async def body():
        pass

    await turn_outcome.run_turn(body(), speaker="A", ts=1.0, discovered_by="wake_word")
    recs = _read_jsonl(path)
    assert recs[0]["route"] == "silent"
    assert recs[0]["executed"] is False


@pytest.mark.asyncio
async def test_dropped_stale_route_not_executed(tmp_path, monkeypatch):
    path = tmp_path / "out.jsonl"
    monkeypatch.setattr(turn_outcome, "OUTCOMES_PATH", path)

    async def body():
        turn_outcome.mark_route("dropped:stale")

    await turn_outcome.run_turn(body(), speaker="A", ts=1.0, discovered_by="wake_word")
    recs = _read_jsonl(path)
    assert recs[0]["route"] == "dropped:stale"
    assert recs[0]["executed"] is False


@pytest.mark.asyncio
async def test_legacy_route_is_executed(tmp_path, monkeypatch):
    path = tmp_path / "out.jsonl"
    monkeypatch.setattr(turn_outcome, "OUTCOMES_PATH", path)

    async def body():
        turn_outcome.mark_route("legacy:status_keywords")

    await turn_outcome.run_turn(body(), speaker="A", ts=1.0, discovered_by="wake_word")
    recs = _read_jsonl(path)
    assert recs[0]["executed"] is True


@pytest.mark.asyncio
async def test_exception_propagates_and_records_error_route(tmp_path, monkeypatch):
    path = tmp_path / "out.jsonl"
    monkeypatch.setattr(turn_outcome, "OUTCOMES_PATH", path)

    async def body():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await turn_outcome.run_turn(body(), speaker="A", ts=1.0, discovered_by="wake_word")

    recs = _read_jsonl(path)
    assert recs[0]["route"] == "error"
    assert recs[0]["executed"] is False


@pytest.mark.asyncio
async def test_cancellation_propagates_and_records_cancelled_route(tmp_path, monkeypatch):
    path = tmp_path / "out.jsonl"
    monkeypatch.setattr(turn_outcome, "OUTCOMES_PATH", path)

    async def body():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await turn_outcome.run_turn(body(), speaker="A", ts=1.0, discovered_by="wake_word")

    recs = _read_jsonl(path)
    assert recs[0]["route"] == "cancelled"
    assert recs[0]["executed"] is False


@pytest.mark.asyncio
async def test_mark_route_after_turn_ends_is_noop(tmp_path, monkeypatch):
    path = tmp_path / "out.jsonl"
    monkeypatch.setattr(turn_outcome, "OUTCOMES_PATH", path)

    async def body():
        turn_outcome.mark_route("inside")

    await turn_outcome.run_turn(body(), speaker="A", ts=1.0, discovered_by="wake_word")
    turn_outcome.mark_route("outside")  # 不應拋、不應改到已寫出的紀錄

    recs = _read_jsonl(path)
    assert recs[0]["route"] == "inside"


def test_mark_route_and_note_winner_outside_turn_do_not_raise():
    turn_outcome.mark_route("x")

    class FakeBid:
        name = "music"
        missing_slots = []
    turn_outcome.note_winner(FakeBid())


def test_append_oserror_is_swallowed_and_throttles_warning(tmp_path, monkeypatch, caplog):
    import logging as _logging

    bad_path = tmp_path / "not_a_dir" / "out.jsonl"
    # 讓 parent 是一個檔案而非目錄，使 mkdir/open 皆失敗 → OSError
    (tmp_path / "not_a_dir").write_text("x")

    with caplog.at_level(_logging.WARNING, logger="turn_outcome"):
        turn_outcome._append(bad_path, {"turn_key": "A@1.000"})
        turn_outcome._append(bad_path, {"turn_key": "A@2.000"})

    warnings = [r for r in caplog.records if r.name == "turn_outcome"]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_turn_route_decorator_marks_route_and_preserves_return(tmp_path, monkeypatch):
    path = tmp_path / "out.jsonl"
    monkeypatch.setattr(turn_outcome, "OUTCOMES_PATH", path)

    @turn_outcome.turn_route("legacy:status_keywords")
    async def handler(x):
        return x * 2

    async def body():
        result = await handler(21)
        assert result == 42

    await turn_outcome.run_turn(body(), speaker="A", ts=1.0, discovered_by="wake_word")
    recs = _read_jsonl(path)
    assert recs[0]["route"] == "legacy:status_keywords"
