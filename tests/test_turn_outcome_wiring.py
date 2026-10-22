"""意圖漏斗步驟 1：TurnOutcome 接線測試
（_process_query_task_locked / _wake_session_on_ignored / IntentBus.dispatch /
_note_gate_decision track 對應）。
"""
from __future__ import annotations

import json

import pytest
from unittest.mock import AsyncMock, MagicMock

import turn_outcome
from cogs.voice_controller_wake_session import WakeSessionMixin
from cogs.voice_controller_turn_outcome import TurnOutcomeMixin
from intent_bus import Bid, IntentBus, IntentContext


def _read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class _FakeVC(WakeSessionMixin):
    def __init__(self, **over):
        self._wake_response_pending = False
        self._wake_session = None
        for k, v in over.items():
            setattr(self, k, v)


# ── _process_query_task_locked 寫 outcome ────────────────────────────────────

@pytest.mark.asyncio
async def test_process_query_task_locked_writes_outcome_with_task_data_discovered_by(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_outcome, "OUTCOMES_PATH", tmp_path / "out.jsonl")
    vc = _FakeVC()

    async def _task(task_data):
        turn_outcome.mark_route("legacy:status_keywords")

    vc._process_query_task = _task
    await vc._process_query_task_locked(
        {"speaker": "showay", "timestamp": 100.0, "discovered_by": "wake_shortcut"})

    recs = _read_jsonl(tmp_path / "out.jsonl")
    assert len(recs) == 1
    assert recs[0]["discovered_by"] == "wake_shortcut"
    assert recs[0]["route"] == "legacy:status_keywords"


@pytest.mark.asyncio
async def test_process_query_task_locked_default_discovered_by_wake_word(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_outcome, "OUTCOMES_PATH", tmp_path / "out.jsonl")
    vc = _FakeVC()
    vc._process_query_task = AsyncMock(return_value=None)

    await vc._process_query_task_locked({"speaker": "showay", "timestamp": 100.0})

    recs = _read_jsonl(tmp_path / "out.jsonl")
    assert recs[0]["discovered_by"] == "wake_word"


@pytest.mark.asyncio
async def test_process_query_task_locked_exception_still_unlocks_and_records_not_executed(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_outcome, "OUTCOMES_PATH", tmp_path / "out.jsonl")
    vc = _FakeVC(_wake_response_pending=True)
    vc._process_query_task = AsyncMock(side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError):
        await vc._process_query_task_locked({"speaker": "showay", "timestamp": 100.0})

    assert vc._wake_response_pending is False
    recs = _read_jsonl(tmp_path / "out.jsonl")
    assert recs[0]["executed"] is False
    assert recs[0]["route"] == "error"


# ── _wake_session_on_ignored → dropped:quality_gate ─────────────────────────

@pytest.mark.asyncio
async def test_wake_session_on_ignored_marks_dropped_quality_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_outcome, "OUTCOMES_PATH", tmp_path / "out.jsonl")
    vc = _FakeVC()

    async def body():
        vc._wake_session_on_ignored("showay")

    await turn_outcome.run_turn(body(), speaker="showay", ts=1.0, discovered_by="wake_word")

    recs = _read_jsonl(tmp_path / "out.jsonl")
    assert recs[0]["route"] == "dropped:quality_gate"
    assert recs[0]["executed"] is False


# ── IntentBus.dispatch → note_winner ─────────────────────────────────────────

class _StubAgent:
    def __init__(self, name, bid_fn):
        self.name = name
        self._bid_fn = bid_fn

    def bid(self, ctx):
        return self._bid_fn(ctx)


def _ctx(query="播周杰倫", speaker="showay"):
    return IntentContext(
        speaker=speaker, raw_text=query, query=query, original_raw=query,
        wake_intent=0.9, stream_active=False, game_mode=False,
        is_owner=False, now=0.0,
    )


@pytest.mark.asyncio
async def test_dispatch_with_winner_records_intent_and_route(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_outcome, "OUTCOMES_PATH", tmp_path / "out.jsonl")
    handler = AsyncMock()
    agent = _StubAgent("music", lambda ctx: Bid("music", 0.9, handler, "regex", missing_slots=[]))
    bus = IntentBus([agent])

    async def body():
        await bus.dispatch(_ctx())

    await turn_outcome.run_turn(body(), speaker="showay", ts=1.0, discovered_by="wake_word")

    recs = _read_jsonl(tmp_path / "out.jsonl")
    assert recs[0]["intent"] == "music"
    assert recs[0]["route"] == "bus:music"


@pytest.mark.asyncio
async def test_dispatch_below_threshold_leaves_intent_none(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_outcome, "OUTCOMES_PATH", tmp_path / "out.jsonl")
    agent = _StubAgent("weak", lambda ctx: Bid("weak", 0.1, AsyncMock(), "regex", missing_slots=[]))
    bus = IntentBus([agent])

    async def body():
        await bus.dispatch(_ctx())

    await turn_outcome.run_turn(body(), speaker="showay", ts=1.0, discovered_by="wake_word")

    recs = _read_jsonl(tmp_path / "out.jsonl")
    assert recs[0]["intent"] is None
    assert recs[0]["route"] == "silent"


# ── _note_gate_decision track 對應 ───────────────────────────────────────────

class _FakeVCGate(TurnOutcomeMixin):
    pass


def test_note_gate_decision_track_mapping(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_outcome, "GATE_PATH", tmp_path / "gate.jsonl")
    vc = _FakeVCGate()

    vc._note_gate_decision("showay", 1.0, "A", False, True, "text", None)
    vc._note_gate_decision("showay", 2.0, None, True, False, "guard_echo", 0.1)
    vc._note_gate_decision("showay", 3.0, "B", False, False, "wake_word", 0.9)
    vc._note_gate_decision("showay", 4.0, None, False, False, "wake_word", 0.9)

    recs = _read_jsonl(tmp_path / "gate.jsonl")
    assert recs[0]["track"] == "text"
    assert recs[1]["track"] == "wake_check"
    assert recs[2]["track"] == "B"
    assert recs[3]["track"] == "A"
