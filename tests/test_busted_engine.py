"""Unit tests for the Busted game modules: scoring, session, and engine."""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from game.scoring import (
    guesser_score,
    setter_score_if_guessed,
    setter_penalty,
    partial_score,
)
from game.session import GameSession, GameState, PlayerState
from game.engine import GameEngine


# ---------------------------------------------------------------------------
# 1. game/scoring.py
# ---------------------------------------------------------------------------

class TestGuesserScore:
    def test_round_1(self):
        assert guesser_score(1) == 100

    def test_round_5(self):
        assert guesser_score(5) == 0


class TestSetterScoreIfGuessed:
    def test_round_1(self):
        assert setter_score_if_guessed(1) == 20

    def test_round_5(self):
        assert setter_score_if_guessed(5) == 100


class TestSetterPenalty:
    def test_penalty_value(self):
        assert setter_penalty() == -100


class TestPartialScore:
    def test_two_of_three_matches(self):
        # 蘋果汁 vs 蘋果醋: positions 0,1 match (蘋,果), position 2 differs (汁 vs 醋)
        assert partial_score("蘋果汁", "蘋果醋") == 66

    def test_exact_match(self):
        assert partial_score("蘋果汁", "蘋果汁") == 100

    def test_one_of_three_matches(self):
        # 蘋果汁 vs 西瓜汁: position 2 matches (汁), others differ
        assert partial_score("蘋果汁", "西瓜汁") == 33

    def test_no_matches(self):
        assert partial_score("abc", "xyz") == 0

    def test_empty_answer_returns_zero(self):
        assert partial_score("", "anything") == 0

    def test_case_insensitive(self):
        assert partial_score("ABC", "abc") == 100


# ---------------------------------------------------------------------------
# 2. game/session.py — construction
# ---------------------------------------------------------------------------

class TestSessionConstruction:
    def test_game_session_defaults(self):
        session = GameSession(session_id="s1", guild_id=123, channel_id=456)
        assert session.state == GameState.IDLE
        assert session.players == []
        assert session.current_setter_id is None
        assert session.current_round == 1

    def test_player_state_defaults(self):
        p = PlayerState(user_id="u1", display_name="Alice")
        assert p.score == 0
        assert p.buzz_cooldown_until == 0.0
        assert p.has_been_setter is False


# ---------------------------------------------------------------------------
# 3. game/engine.py — async state-machine tests
# ---------------------------------------------------------------------------

def make_engine(on_change=None, judge_fn=None, clue_fn=None):
    session = GameSession(session_id="test-1", guild_id=1, channel_id=1)
    if on_change is None:
        on_change = AsyncMock()
    return GameEngine(
        session,
        on_state_change=on_change,
        judge_fn=judge_fn,
        clue_fn=clue_fn,
        db_path=":memory:",
    )


@pytest.mark.asyncio
async def test_add_player_success():
    engine = make_engine()
    await asyncio.sleep(0.05)
    result = await engine.add_player("u1", "Alice")
    assert result is True
    assert engine._get_player("u1") is not None


@pytest.mark.asyncio
async def test_add_player_duplicate():
    engine = make_engine()
    await asyncio.sleep(0.05)
    await engine.add_player("u1", "Alice")
    result = await engine.add_player("u1", "Alice Again")
    assert result is False


@pytest.mark.asyncio
async def test_add_player_max():
    engine = make_engine()
    await asyncio.sleep(0.05)
    # Add 5 humans (MAX_HUMAN_PLAYERS)
    for i in range(5):
        ok = await engine.add_player(f"u{i}", f"Player{i}")
        assert ok is True, f"Expected player {i} to be added"
    # 6th human should be rejected
    result = await engine.add_player("u5", "Player5")
    assert result is False


@pytest.mark.asyncio
async def test_start_game():
    engine = make_engine()
    await asyncio.sleep(0.05)
    await engine.add_player("u1", "Alice")
    await engine.start_game()
    assert engine.session.state == GameState.SPINNING


@pytest.mark.asyncio
async def test_set_answer():
    engine = make_engine()
    await asyncio.sleep(0.05)
    await engine.add_player("u1", "Alice")
    await engine.start_game()
    await engine.begin_setter_input()
    engine.session.current_setter_id = "u1"
    await engine.set_answer("蘋果汁")
    assert engine.session.state == GameState.CLUE_ACTIVE
    assert engine.session.current_answer == "蘋果汁"


@pytest.mark.asyncio
async def test_buzz_in_success():
    engine = make_engine()
    await asyncio.sleep(0.05)
    await engine.add_player("u1", "Alice")
    await engine.add_player("u2", "Bob")
    await engine.start_game()
    await engine.begin_setter_input()
    engine.session.current_setter_id = "u1"
    await engine.set_answer("蘋果汁")
    # u2 (non-setter) buzzes in
    result = await engine.buzz_in("u2")
    assert result is True
    assert engine.session.state == GameState.BUZZ_LOCKED


@pytest.mark.asyncio
async def test_buzz_in_setter_blocked():
    engine = make_engine()
    await asyncio.sleep(0.05)
    await engine.add_player("u1", "Alice")
    await engine.add_player("u2", "Bob")
    await engine.start_game()
    await engine.begin_setter_input()
    engine.session.current_setter_id = "u1"
    await engine.set_answer("蘋果汁")
    # u1 is setter — should be blocked
    result = await engine.buzz_in("u1")
    assert result is False


@pytest.mark.asyncio
async def test_submit_answer_correct():
    judge = AsyncMock(return_value=True)
    engine = make_engine(judge_fn=judge)
    await asyncio.sleep(0.05)
    await engine.add_player("u1", "Alice")
    await engine.add_player("u2", "Bob")
    await engine.start_game()
    await engine.begin_setter_input()
    engine.session.current_setter_id = "u1"
    await engine.set_answer("蘋果汁")
    await engine.buzz_in("u2")
    result = await engine.submit_answer("u2", "蘋果汁")
    assert result["correct"] is True
    # u2 should have gained guesser points (round 1 = 100)
    player = engine._get_player("u2")
    assert player.score > 0


@pytest.mark.asyncio
async def test_submit_answer_wrong():
    judge = AsyncMock(return_value=False)
    engine = make_engine(judge_fn=judge)
    await asyncio.sleep(0.05)
    await engine.add_player("u1", "Alice")
    await engine.add_player("u2", "Bob")
    await engine.start_game()
    await engine.begin_setter_input()
    engine.session.current_setter_id = "u1"
    await engine.set_answer("蘋果汁")
    await engine.buzz_in("u2")
    result = await engine.submit_answer("u2", "西瓜汁")
    assert result["correct"] is False
    # u2 should have a cooldown set
    player = engine._get_player("u2")
    import time
    assert player.buzz_cooldown_until > time.time()


@pytest.mark.asyncio
async def test_advance_clue_increments():
    engine = make_engine()
    await asyncio.sleep(0.05)
    await engine.add_player("u1", "Alice")
    await engine.start_game()
    await engine.begin_setter_input()
    engine.session.current_setter_id = "u1"
    await engine.set_answer("蘋果汁")
    assert engine.session.current_round == 1
    await engine.advance_clue()
    assert engine.session.current_round == 2


@pytest.mark.asyncio
async def test_advance_clue_penalty():
    engine = make_engine()
    await asyncio.sleep(0.05)
    await engine.add_player("u1", "Alice")
    await engine.start_game()
    await engine.begin_setter_input()
    engine.session.current_setter_id = "u1"
    await engine.set_answer("蘋果汁")
    # Fast-forward to round 5 and ensure no round5 scores exist
    engine.session.current_round = 5
    engine._round5_scores.clear()
    # Advance again → round 5 window expired → setter gets penalised
    await engine.advance_clue()
    setter = engine._get_player("u1")
    assert setter.score == -100
    assert engine.session.state == GameState.ROUND_RESULT


@pytest.mark.asyncio
async def test_expire_buzz():
    engine = make_engine()
    await asyncio.sleep(0.05)
    await engine.add_player("u1", "Alice")
    await engine.add_player("u2", "Bob")
    await engine.start_game()
    await engine.begin_setter_input()
    engine.session.current_setter_id = "u1"
    await engine.set_answer("蘋果汁")
    await engine.buzz_in("u2")
    assert engine.session.state == GameState.BUZZ_LOCKED
    await engine.expire_buzz()
    assert engine.session.state == GameState.CLUE_ACTIVE


@pytest.mark.asyncio
async def test_next_round_advances():
    judge = AsyncMock(return_value=True)
    engine = make_engine(judge_fn=judge)
    await asyncio.sleep(0.05)
    await engine.add_player("u1", "Alice")
    await engine.add_player("u2", "Bob")
    await engine.start_game()
    await engine.begin_setter_input()
    engine.session.current_setter_id = "u1"
    # Drain u1 from remaining_setters so only u2 remains
    engine.session.remaining_setters = ["u2"]
    await engine.set_answer("蘋果汁")
    await engine.buzz_in("u2")
    await engine.submit_answer("u2", "蘋果汁")
    assert engine.session.state == GameState.ROUND_RESULT
    result = await engine.next_round()
    assert result is True
    assert engine.session.state == GameState.SPINNING


@pytest.mark.asyncio
async def test_next_round_game_over():
    engine = make_engine()
    await asyncio.sleep(0.05)
    # Add only marvin (simulate single-player / all setters exhausted scenario)
    engine.session.players.append(PlayerState(user_id="marvin", display_name="Marvin"))
    engine.session.remaining_setters = []
    engine.session.current_setter_id = "marvin"
    # Mark all players as having been setter
    for p in engine.session.players:
        p.has_been_setter = True
    # Force state to ROUND_RESULT so next_round() runs
    engine.session.state = GameState.ROUND_RESULT
    result = await engine.next_round()
    assert result is False
    assert engine.session.state == GameState.GAME_OVER
