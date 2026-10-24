"""turn_outcome.py 的 note_gesture_hint / claim_first_speech 單元測試（puck 動作 2b）。"""
from __future__ import annotations

import json

import pytest

import turn_outcome


def _read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_note_gesture_hint_noop_outside_turn():
    turn_outcome.note_gesture_hint("Y")  # 不丟例外即可


def test_claim_first_speech_noop_outside_turn():
    assert turn_outcome.claim_first_speech() is None


@pytest.mark.asyncio
async def test_claim_first_speech_only_succeeds_once_per_turn(tmp_path, monkeypatch):
    path = tmp_path / "outcomes.jsonl"
    monkeypatch.setattr(turn_outcome, "OUTCOMES_PATH", path)

    results = []

    async def body():
        results.append(turn_outcome.claim_first_speech())
        results.append(turn_outcome.claim_first_speech())
        results.append(turn_outcome.claim_first_speech())

    await turn_outcome.run_turn(body(), speaker="A", ts=1.0, discovered_by="wake_word")

    assert results[0] is not None
    assert results[1] is None
    assert results[2] is None


@pytest.mark.asyncio
async def test_outcomes_jsonl_excludes_underscore_keys(tmp_path, monkeypatch):
    path = tmp_path / "outcomes.jsonl"
    monkeypatch.setattr(turn_outcome, "OUTCOMES_PATH", path)

    async def body():
        turn_outcome.note_gesture_hint("Y")
        turn_outcome.claim_first_speech()
        turn_outcome.mark_route("bus:music")

    await turn_outcome.run_turn(body(), speaker="A", ts=1.0, discovered_by="wake_word")

    recs = _read_jsonl(path)
    assert len(recs) == 1
    assert not any(k.startswith("_") for k in recs[0])
    assert recs[0]["route"] == "bus:music"
