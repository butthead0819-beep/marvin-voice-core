"""TDD tests for the Detective (Two Truths One Lie) game engine."""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock

from game.detective.session import (
    DetectiveSession,
    DetectiveState,
    PlayerDState,
    StatementSet,
)
from game.detective.engine import DetectiveEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_session() -> DetectiveSession:
    return DetectiveSession(
        session_id="test-session",
        guild_id=111,
        channel_id=222,
    )


def make_engine(session: DetectiveSession | None = None) -> tuple[DetectiveEngine, AsyncMock]:
    if session is None:
        session = make_session()
    cb = AsyncMock()
    engine = DetectiveEngine(session, on_state_change=cb, db_path=":memory:")
    return engine, cb


# ---------------------------------------------------------------------------
# 1. add_player
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_player_succeeds():
    engine, cb = make_engine()
    result = await engine.add_player("u1", "Alice")
    assert result is True
    assert len(engine.session.players) == 1
    assert engine.session.players[0].user_id == "u1"
    assert engine.session.state == DetectiveState.JOINING


@pytest.mark.asyncio
async def test_add_player_rejects_duplicate():
    engine, cb = make_engine()
    await engine.add_player("u1", "Alice")
    result = await engine.add_player("u1", "Alice Again")
    assert result is False
    assert len(engine.session.players) == 1


@pytest.mark.asyncio
async def test_add_player_rejects_when_game_active():
    engine, cb = make_engine()
    # Add 3 players and start game so state = DECLARING
    await engine.add_player("u1", "Alice")
    await engine.add_player("u2", "Bob")
    await engine.add_player("u3", "Charlie")
    await engine.start_game()
    # Now try to add a new player
    result = await engine.add_player("u4", "Dave")
    assert result is False


# ---------------------------------------------------------------------------
# 2. start_game
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_game_requires_min_3_players():
    engine, cb = make_engine()
    await engine.add_player("u1", "Alice")
    await engine.add_player("u2", "Bob")
    result = await engine.start_game()
    assert result is False
    assert engine.session.state == DetectiveState.JOINING


@pytest.mark.asyncio
async def test_start_game_sets_declaring_state():
    engine, cb = make_engine()
    await engine.add_player("u1", "Alice")
    await engine.add_player("u2", "Bob")
    await engine.add_player("u3", "Charlie")
    result = await engine.start_game()
    assert result is True
    assert engine.session.state == DetectiveState.DECLARING


@pytest.mark.asyncio
async def test_start_game_sets_declarer_queue():
    engine, cb = make_engine()
    await engine.add_player("u1", "Alice")
    await engine.add_player("u2", "Bob")
    await engine.add_player("u3", "Charlie")
    await engine.start_game()
    # current_declarer_id should be set, and declarer_queue should have the remaining 2
    assert engine.session.current_declarer_id is not None
    # Total = 3 players: 1 current + 2 in queue (or queue has remaining after pop)
    total_remaining = len(engine.session.declarer_queue) + 1  # +1 for current
    assert total_remaining == 3


# ---------------------------------------------------------------------------
# 3. submit_statements
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_submit_statements_valid():
    engine, cb = make_engine()
    await engine.add_player("u1", "Alice")
    await engine.add_player("u2", "Bob")
    await engine.add_player("u3", "Charlie")
    await engine.start_game()
    declarer_id = engine.session.current_declarer_id
    result = await engine.submit_statements(declarer_id, "I love cats", "I can fly", "I play piano", 1)
    assert result is True
    assert engine.session.state == DetectiveState.VOTING
    assert engine.session.current_statements is not None
    assert engine.session.current_statements.lie_index == 1


@pytest.mark.asyncio
async def test_submit_statements_wrong_declarer_rejected():
    engine, cb = make_engine()
    await engine.add_player("u1", "Alice")
    await engine.add_player("u2", "Bob")
    await engine.add_player("u3", "Charlie")
    await engine.start_game()
    # Find a non-declarer
    declarer_id = engine.session.current_declarer_id
    non_declarer = next(p.user_id for p in engine.session.players if p.user_id != declarer_id)
    result = await engine.submit_statements(non_declarer, "I love cats", "I can fly", "I play piano", 0)
    assert result is False
    assert engine.session.state == DetectiveState.DECLARING


@pytest.mark.asyncio
async def test_submit_statements_invalid_lie_index():
    engine, cb = make_engine()
    await engine.add_player("u1", "Alice")
    await engine.add_player("u2", "Bob")
    await engine.add_player("u3", "Charlie")
    await engine.start_game()
    declarer_id = engine.session.current_declarer_id
    result = await engine.submit_statements(declarer_id, "A", "B", "C", 3)  # invalid: 3
    assert result is False
    assert engine.session.state == DetectiveState.DECLARING


# ---------------------------------------------------------------------------
# 4. submit_vote
# ---------------------------------------------------------------------------

async def _setup_voting(n_players: int = 3) -> tuple[DetectiveEngine, AsyncMock, str]:
    """Helper: create engine, add n players, start game, submit statements, return (engine, cb, declarer_id)."""
    engine, cb = make_engine()
    ids = [f"u{i}" for i in range(1, n_players + 1)]
    names = [f"Player{i}" for i in range(1, n_players + 1)]
    for uid, name in zip(ids, names):
        await engine.add_player(uid, name)
    await engine.start_game()
    declarer_id = engine.session.current_declarer_id
    await engine.submit_statements(declarer_id, "Truth A", "Truth B", "Lie C", 2)
    return engine, cb, declarer_id


@pytest.mark.asyncio
async def test_submit_vote_valid():
    engine, cb, declarer_id = await _setup_voting()
    voter_id = next(p.user_id for p in engine.session.players if p.user_id != declarer_id)
    result = await engine.submit_vote(voter_id, 2)
    assert "error" not in result
    assert result.get("already_voted") is False
    player = next(p for p in engine.session.players if p.user_id == voter_id)
    assert player.vote == 2


@pytest.mark.asyncio
async def test_submit_vote_already_voted():
    engine, cb, declarer_id = await _setup_voting()
    voter_id = next(p.user_id for p in engine.session.players if p.user_id != declarer_id)
    await engine.submit_vote(voter_id, 0)
    result = await engine.submit_vote(voter_id, 1)
    assert result.get("already_voted") is True
    # Vote should not change
    player = next(p for p in engine.session.players if p.user_id == voter_id)
    assert player.vote == 0


@pytest.mark.asyncio
async def test_submit_vote_declarer_cannot_vote():
    engine, cb, declarer_id = await _setup_voting()
    result = await engine.submit_vote(declarer_id, 0)
    assert result.get("error") == "invalid_voter"


@pytest.mark.asyncio
async def test_submit_vote_all_voted_flag():
    engine, cb, declarer_id = await _setup_voting(n_players=3)
    voters = [p.user_id for p in engine.session.players if p.user_id != declarer_id]
    # All voters except the last
    for voter_id in voters[:-1]:
        result = await engine.submit_vote(voter_id, 1)
        assert result.get("all_voted") is False
    # Last voter
    result = await engine.submit_vote(voters[-1], 1)
    assert result.get("all_voted") is True


# ---------------------------------------------------------------------------
# 5. close_voting
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_voting_scoring_correct_voter():
    engine, cb, declarer_id = await _setup_voting(n_players=3)
    voters = [p.user_id for p in engine.session.players if p.user_id != declarer_id]
    # Correct lie_index is 2
    await engine.submit_vote(voters[0], 2)  # correct
    result = await engine.close_voting()
    score_changes = result["score_changes"]
    assert score_changes.get(voters[0]) == 50


@pytest.mark.asyncio
async def test_close_voting_scoring_declarer_per_fool():
    engine, cb, declarer_id = await _setup_voting(n_players=3)
    voters = [p.user_id for p in engine.session.players if p.user_id != declarer_id]
    # Both voters vote wrong (lie_index=2, vote 0)
    await engine.submit_vote(voters[0], 0)  # wrong
    await engine.submit_vote(voters[1], 0)  # wrong
    result = await engine.close_voting()
    score_changes = result["score_changes"]
    # 2 fooled voters → declarer gets 2 * 30 = 60
    assert score_changes.get(declarer_id) == 60


@pytest.mark.asyncio
async def test_close_voting_state_revealing():
    engine, cb, declarer_id = await _setup_voting(n_players=3)
    voters = [p.user_id for p in engine.session.players if p.user_id != declarer_id]
    await engine.submit_vote(voters[0], 2)
    await engine.close_voting()
    assert engine.session.state == DetectiveState.REVEALING


# ---------------------------------------------------------------------------
# 6. skip_declaring
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_skip_declaring_marks_has_declared():
    engine, cb = make_engine()
    await engine.add_player("u1", "Alice")
    await engine.add_player("u2", "Bob")
    await engine.add_player("u3", "Charlie")
    await engine.start_game()
    declarer_id = engine.session.current_declarer_id
    await engine.skip_declaring()
    declarer = next(p for p in engine.session.players if p.user_id == declarer_id)
    assert declarer.has_declared is True


# ---------------------------------------------------------------------------
# 7. advance_declaring
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_advance_declaring_cycles_to_next():
    engine, cb = make_engine()
    await engine.add_player("u1", "Alice")
    await engine.add_player("u2", "Bob")
    await engine.add_player("u3", "Charlie")
    await engine.start_game()
    first_declarer = engine.session.current_declarer_id
    # Put engine in REVEALING by completing a vote round
    await engine.submit_statements(first_declarer, "A", "B", "C", 0)
    voters = [p.user_id for p in engine.session.players if p.user_id != first_declarer]
    for voter in voters:
        await engine.submit_vote(voter, 0)
    await engine.close_voting()  # state -> REVEALING
    result = await engine.advance_declaring()
    assert result is True
    assert engine.session.current_declarer_id != first_declarer
    assert engine.session.state == DetectiveState.DECLARING


@pytest.mark.asyncio
async def test_advance_declaring_game_over_when_queue_empty():
    engine, cb = make_engine()
    await engine.add_player("u1", "Alice")
    await engine.add_player("u2", "Bob")
    await engine.add_player("u3", "Charlie")
    await engine.start_game()

    # Force queue empty: manually clear the queue and set state to REVEALING
    engine.session.declarer_queue.clear()
    engine.session.state = DetectiveState.REVEALING
    result = await engine.advance_declaring()
    assert result is False
    assert engine.session.state == DetectiveState.GAME_OVER


# ---------------------------------------------------------------------------
# 8. Full game (3 players, 3 rounds, ends in GAME_OVER)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_game_3_players():
    engine, cb = make_engine()
    await engine.add_player("u1", "Alice")
    await engine.add_player("u2", "Bob")
    await engine.add_player("u3", "Charlie")
    await engine.start_game()
    assert engine.session.state == DetectiveState.DECLARING

    for _round in range(3):
        declarer_id = engine.session.current_declarer_id
        await engine.submit_statements(declarer_id, "Truth A", "Truth B", "Lie C", 2)
        assert engine.session.state == DetectiveState.VOTING

        voters = [p.user_id for p in engine.session.players if p.user_id != declarer_id]
        for voter in voters:
            await engine.submit_vote(voter, 2)  # correct votes
        await engine.close_voting()
        assert engine.session.state == DetectiveState.REVEALING

        continues = await engine.advance_declaring()
        if _round < 2:
            assert continues is True
            assert engine.session.state == DetectiveState.DECLARING
        else:
            assert continues is False
            assert engine.session.state == DetectiveState.GAME_OVER
