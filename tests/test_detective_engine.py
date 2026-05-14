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


# ---------------------------------------------------------------------------
# 9. Edge cases — 投票邊界
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_submit_vote_invalid_index_rejected():
    """vote_index 不是 0/1/2 應被拒絕，回傳 error invalid_vote_index。"""
    engine, cb, declarer_id = await _setup_voting()
    voter_id = next(p.user_id for p in engine.session.players if p.user_id != declarer_id)
    result = await engine.submit_vote(voter_id, 3)
    assert result.get("error") == "invalid_vote_index"
    # 投票應未被記錄
    player = next(p for p in engine.session.players if p.user_id == voter_id)
    assert player.vote is None


@pytest.mark.asyncio
async def test_submit_vote_out_of_voting_state():
    """非 VOTING 狀態投票應被拒絕，回傳 error invalid_state。"""
    engine, cb = make_engine()
    await engine.add_player("u1", "Alice")
    await engine.add_player("u2", "Bob")
    await engine.add_player("u3", "Charlie")
    await engine.start_game()
    # 此時狀態是 DECLARING，非 VOTING
    assert engine.session.state == DetectiveState.DECLARING
    result = await engine.submit_vote("u2", 0)
    assert result.get("error") == "invalid_state"


@pytest.mark.asyncio
async def test_close_voting_with_no_votes():
    """所有人都沒投票，陳述者應得 +0（無人被騙）。"""
    engine, cb, declarer_id = await _setup_voting(n_players=3)
    # 不讓任何人投票，直接 close_voting
    result = await engine.close_voting()
    assert "error" not in result
    assert result["fooled_voters"] == []
    assert result["correct_voters"] == []
    # 陳述者不應得分
    assert result["score_changes"].get(declarer_id, 0) == 0
    declarer = next(p for p in engine.session.players if p.user_id == declarer_id)
    assert declarer.score == 0


@pytest.mark.asyncio
async def test_close_voting_called_twice():
    """close_voting 被呼叫兩次，第二次應回 error invalid_state，不重複計分。"""
    engine, cb, declarer_id = await _setup_voting(n_players=3)
    voters = [p.user_id for p in engine.session.players if p.user_id != declarer_id]
    # 兩人都投錯
    await engine.submit_vote(voters[0], 0)
    await engine.submit_vote(voters[1], 0)
    # 第一次 close_voting
    result1 = await engine.close_voting()
    assert "error" not in result1
    declarer = next(p for p in engine.session.players if p.user_id == declarer_id)
    score_after_first = declarer.score
    # 第二次 close_voting（狀態已是 REVEALING）
    result2 = await engine.close_voting()
    assert result2.get("error") == "invalid_state"
    # 分數不應改變
    assert declarer.score == score_after_first


# ---------------------------------------------------------------------------
# 10. Edge cases — 跳過邊界
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_skip_declaring_in_wrong_state():
    """非 DECLARING 狀態 skip 應回 False。"""
    engine, cb = make_engine()
    await engine.add_player("u1", "Alice")
    await engine.add_player("u2", "Bob")
    await engine.add_player("u3", "Charlie")
    await engine.start_game()
    declarer_id = engine.session.current_declarer_id
    # 先推進到 VOTING
    await engine.submit_statements(declarer_id, "A", "B", "C", 0)
    assert engine.session.state == DetectiveState.VOTING
    # 在 VOTING 狀態呼叫 skip_declaring 應回 False
    result = await engine.skip_declaring()
    assert result is False
    assert engine.session.state == DetectiveState.VOTING


@pytest.mark.asyncio
async def test_skip_declaring_advances_to_next():
    """skip 後應換到下一個陳述者（不是 GAME_OVER 時）。"""
    engine, cb = make_engine()
    await engine.add_player("u1", "Alice")
    await engine.add_player("u2", "Bob")
    await engine.add_player("u3", "Charlie")
    await engine.start_game()
    first_declarer = engine.session.current_declarer_id
    result = await engine.skip_declaring()
    # 還有人在隊列，應繼續遊戲
    assert result is True
    assert engine.session.state == DetectiveState.DECLARING
    assert engine.session.current_declarer_id != first_declarer


@pytest.mark.asyncio
async def test_skip_last_declarer():
    """最後一個陳述者也被跳過，應進入 GAME_OVER。"""
    engine, cb = make_engine()
    await engine.add_player("u1", "Alice")
    await engine.add_player("u2", "Bob")
    await engine.add_player("u3", "Charlie")
    await engine.start_game()
    # 清空隊列，只剩目前陳述者
    engine.session.declarer_queue.clear()
    result = await engine.skip_declaring()
    assert result is False
    assert engine.session.state == DetectiveState.GAME_OVER


# ---------------------------------------------------------------------------
# 11. Edge cases — 計分邊界
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_voting_all_correct():
    """所有人猜中，陳述者 +0，每個猜中者 +50。"""
    engine, cb, declarer_id = await _setup_voting(n_players=4)
    voters = [p.user_id for p in engine.session.players if p.user_id != declarer_id]
    # lie_index=2，所有人都投 2
    for voter_id in voters:
        await engine.submit_vote(voter_id, 2)
    result = await engine.close_voting()
    assert "error" not in result
    assert set(result["correct_voters"]) == set(voters)
    assert result["fooled_voters"] == []
    # 陳述者 +0
    assert result["score_changes"].get(declarer_id, 0) == 0
    declarer = next(p for p in engine.session.players if p.user_id == declarer_id)
    assert declarer.score == 0
    # 每個猜中者 +50
    for voter_id in voters:
        voter = next(p for p in engine.session.players if p.user_id == voter_id)
        assert voter.score == 50
        assert result["score_changes"].get(voter_id) == 50


@pytest.mark.asyncio
async def test_close_voting_all_wrong():
    """所有人猜錯，陳述者 +30*(人數-1)，每個猜錯者 +0。"""
    engine, cb, declarer_id = await _setup_voting(n_players=4)
    voters = [p.user_id for p in engine.session.players if p.user_id != declarer_id]
    # lie_index=2，所有人都投 0（錯的）
    for voter_id in voters:
        await engine.submit_vote(voter_id, 0)
    result = await engine.close_voting()
    assert "error" not in result
    assert result["correct_voters"] == []
    assert set(result["fooled_voters"]) == set(voters)
    # 陳述者 +30 * 3 = 90
    expected_declarer_pts = 30 * len(voters)
    assert result["score_changes"].get(declarer_id) == expected_declarer_pts
    declarer = next(p for p in engine.session.players if p.user_id == declarer_id)
    assert declarer.score == expected_declarer_pts
    # 每個猜錯者 +0
    for voter_id in voters:
        voter = next(p for p in engine.session.players if p.user_id == voter_id)
        assert voter.score == 0
        assert voter_id not in result["score_changes"]


# ---------------------------------------------------------------------------
# 12. Edge cases — 玩家行為
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_player_max_8():
    """加入第 9 個人應回 False。"""
    engine, cb = make_engine()
    for i in range(1, 9):
        result = await engine.add_player(f"u{i}", f"Player{i}")
        assert result is True
    assert len(engine.session.players) == 8
    # 第 9 個應被拒絕
    result = await engine.add_player("u9", "Player9")
    assert result is False
    assert len(engine.session.players) == 8


@pytest.mark.asyncio
async def test_submit_statements_in_wrong_state():
    """非 DECLARING 狀態提交陳述應回 False。"""
    engine, cb, declarer_id = await _setup_voting()
    # 現在狀態是 VOTING
    assert engine.session.state == DetectiveState.VOTING
    # 陳述者嘗試再次提交陳述
    result = await engine.submit_statements(declarer_id, "A", "B", "C", 1)
    assert result is False
    assert engine.session.state == DetectiveState.VOTING
