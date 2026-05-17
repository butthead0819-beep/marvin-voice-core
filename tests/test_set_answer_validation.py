"""TDD — engine.set_answer length validation.

Backstory: Marvin's auto-setter path bypasses SetAnswerModal, so the only
guardrail against a 0/1-char or 6+ char answer was in modal-land. If the
LLM hallucinates an empty string or 13-char output, the engine would
silently accept it and the game would CLUE_ACTIVE on an unplayable
answer. set_answer is the choke point — enforce length there.

Tests:
  A) set_answer with len 2..5 → accepts, state → CLUE_ACTIVE
  B) set_answer with len 1   → rejects (returns False), state stays SETTER_INPUT
  C) set_answer with len 6   → rejects
  D) set_answer with empty   → rejects
  E) set_answer with whitespace-only → rejects (after strip)
  F) When rejected, no clue_fn is fired
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from game.engine import GameEngine, ANSWER_MIN_LEN, ANSWER_MAX_LEN
from game.session import GameSession, GameState, PlayerState


def _make_session() -> GameSession:
    s = GameSession(session_id="t", guild_id=1, channel_id=1)
    s.players = [
        PlayerState(user_id="setter", display_name="出題人"),
        PlayerState(user_id="u1", display_name="狗與露"),
    ]
    s.current_setter_id = "setter"
    s.state = GameState.SETTER_INPUT
    return s


def _make_engine(session: GameSession, clue_fn=None) -> GameEngine:
    return GameEngine(
        session=session,
        on_state_change=AsyncMock(),
        db_path=":memory:",
        clue_fn=clue_fn,
    )


# ── A: valid length accepts ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_answer_accepts_min_length():
    s = _make_session()
    engine = _make_engine(s)
    await engine.set_answer("黑洞")
    assert s.current_answer == "黑洞"
    assert s.state == GameState.CLUE_ACTIVE


@pytest.mark.asyncio
async def test_set_answer_accepts_max_length():
    s = _make_session()
    engine = _make_engine(s)
    await engine.set_answer("巨石強森王")  # 5 chars
    assert s.current_answer == "巨石強森王"
    assert s.state == GameState.CLUE_ACTIVE


# ── B/C/D/E: invalid length rejects ────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_answer_rejects_one_char():
    s = _make_session()
    engine = _make_engine(s)
    await engine.set_answer("愛")
    assert s.current_answer is None
    assert s.state == GameState.SETTER_INPUT


@pytest.mark.asyncio
async def test_set_answer_rejects_six_chars():
    s = _make_session()
    engine = _make_engine(s)
    await engine.set_answer("一二三四五六")
    assert s.current_answer is None
    assert s.state == GameState.SETTER_INPUT


@pytest.mark.asyncio
async def test_set_answer_rejects_empty():
    s = _make_session()
    engine = _make_engine(s)
    await engine.set_answer("")
    assert s.current_answer is None
    assert s.state == GameState.SETTER_INPUT


@pytest.mark.asyncio
async def test_set_answer_rejects_whitespace_only():
    s = _make_session()
    engine = _make_engine(s)
    await engine.set_answer("   ")
    assert s.current_answer is None
    assert s.state == GameState.SETTER_INPUT


# ── F: rejected answer does not fire clue_fn ──────────────────────────────

@pytest.mark.asyncio
async def test_set_answer_rejected_does_not_trigger_clue_fn():
    s = _make_session()
    clue_fn = AsyncMock()
    engine = _make_engine(s, clue_fn=clue_fn)
    await engine.set_answer("X")  # 1 char, rejected
    # Give the create_task a chance to schedule (it shouldn't)
    import asyncio
    await asyncio.sleep(0)
    clue_fn.assert_not_called()
