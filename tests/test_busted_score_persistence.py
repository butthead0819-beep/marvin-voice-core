"""
TDD for immediate score persistence:
Scores must be written to player_scores on every score change,
not only at game end — so disconnects don't lose earned scores.

Covered events:
1. submit_answer correct → persists guesser + setter pts immediately
2. advance_clue (R5 expire) → persists setter pts immediately
3. submit_round5_answer → persists player pts immediately
4. skip_setter_timeout → persists setter penalty immediately
5. Full game: no double-count (scores match expected total)
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile

import pytest
from unittest.mock import AsyncMock

from game.session import GameSession, GameState, PlayerState
from game.engine import GameEngine, SETTER_TIMEOUT_PENALTY


# ── helpers ───────────────────────────────────────────────────────────────────

async def _make_engine(db_path: str, judge_correct: bool = True) -> GameEngine:
    engine = GameEngine(
        GameSession(session_id="t1", guild_id=1, channel_id=1),
        on_state_change=AsyncMock(),
        db_path=db_path,
        judge_fn=AsyncMock(return_value=judge_correct),
    )
    await asyncio.sleep(0.05)  # let _init_db executor thread complete
    return engine


def _query_scores(db_path: str) -> dict[str, int]:
    con = sqlite3.connect(db_path)
    rows = con.execute("SELECT user_id, score FROM player_scores").fetchall()
    con.close()
    return dict(rows)


# ── 1. Correct answer persists immediately ────────────────────────────────────

@pytest.mark.asyncio
async def test_correct_answer_persists_guesser_score_immediately():
    """Guesser's score should appear in player_scores right after correct answer."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        engine = await _make_engine(db_path, judge_correct=True)
        session = engine.session
        session.players = [
            PlayerState(user_id="setter", display_name="A"),
            PlayerState(user_id="g1", display_name="B"),
        ]
        session.state = GameState.CLUE_ACTIVE
        session.current_setter_id = "setter"
        await engine.set_answer("蘋果汁")
        await engine.buzz_in("g1")
        await engine.submit_answer("g1", "蘋果汁")

        await asyncio.sleep(0.15)  # let executor thread complete

        scores = _query_scores(db_path)
        assert scores.get("g1", 0) > 0, "guesser score should be persisted immediately after correct answer"
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_correct_answer_persists_setter_score_immediately():
    """Setter's score should appear in player_scores right after correct answer."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        engine = await _make_engine(db_path, judge_correct=True)
        session = engine.session
        session.players = [
            PlayerState(user_id="setter", display_name="A"),
            PlayerState(user_id="g1", display_name="B"),
        ]
        session.state = GameState.CLUE_ACTIVE
        session.current_setter_id = "setter"
        await engine.set_answer("蘋果汁")
        await engine.buzz_in("g1")
        await engine.submit_answer("g1", "蘋果汁")

        await asyncio.sleep(0.15)

        scores = _query_scores(db_path)
        assert scores.get("setter", 0) > 0, "setter score should be persisted immediately after correct answer"
    finally:
        os.unlink(db_path)


# ── 2. submit_round5_answer persists immediately ──────────────────────────────

@pytest.mark.asyncio
async def test_round5_answer_persists_score_immediately():
    """Partial score from round 5 should appear in player_scores before advance_clue."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        engine = await _make_engine(db_path)
        session = engine.session
        session.players = [
            PlayerState(user_id="setter", display_name="A"),
            PlayerState(user_id="g1", display_name="B"),
        ]
        session.state = GameState.CLUE_ACTIVE
        session.current_setter_id = "setter"
        await engine.set_answer("蘋果汁")
        engine.session.current_round = 5  # force round 5

        result = await engine.submit_round5_answer("g1", "蘋果水")  # 蘋 matches → pts > 0

        await asyncio.sleep(0.15)

        scores = _query_scores(db_path)
        expected_pts = result["pts"]
        if expected_pts > 0:
            assert scores.get("g1", 0) == expected_pts, (
                f"player_scores['g1'] should be {expected_pts}, got {scores.get('g1', 0)}"
            )
    finally:
        os.unlink(db_path)


# ── 3. advance_clue R5 expire persists setter score immediately ───────────────

@pytest.mark.asyncio
async def test_r5_expire_persists_setter_score_immediately():
    """When round 5 expires via advance_clue, setter's score should be persisted."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        engine = await _make_engine(db_path)
        session = engine.session
        session.players = [
            PlayerState(user_id="setter", display_name="A"),
            PlayerState(user_id="g1", display_name="B"),
        ]
        session.state = GameState.CLUE_ACTIVE
        session.current_setter_id = "setter"
        await engine.set_answer("蘋果汁")
        engine.session.current_round = 5  # force round 5

        # g1 submits (any score), then expire — setter should get 100 pts
        await engine.submit_round5_answer("g1", "蘋果汁")  # full match → pts > 0
        await engine.advance_clue()  # finalise R5

        await asyncio.sleep(0.15)

        scores = _query_scores(db_path)
        # setter should have 100 pts (any_scored is True)
        assert scores.get("setter", 0) > 0, "setter score should be persisted after R5 expire"
    finally:
        os.unlink(db_path)


# ── 4. skip_setter_timeout persists penalty immediately ───────────────────────

@pytest.mark.asyncio
async def test_setter_timeout_persists_penalty_immediately():
    """SETTER_TIMEOUT_PENALTY should appear in player_scores right after timeout."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        engine = await _make_engine(db_path)
        session = engine.session
        session.players = [
            PlayerState(user_id="setter", display_name="A"),
            PlayerState(user_id="g1", display_name="B"),
        ]
        session.state = GameState.SETTER_INPUT
        session.current_setter_id = "setter"
        session.remaining_setters = ["g1"]  # so next_setter != None

        await engine.skip_setter_timeout()

        await asyncio.sleep(0.15)

        scores = _query_scores(db_path)
        # SETTER_TIMEOUT_PENALTY is negative (-50), so score should be negative
        assert "setter" in scores, "setter should appear in player_scores after timeout"
        assert scores["setter"] == SETTER_TIMEOUT_PENALTY, (
            f"expected {SETTER_TIMEOUT_PENALTY}, got {scores['setter']}"
        )
    finally:
        os.unlink(db_path)


# ── 5. No double-count at game end ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_double_count_after_full_game():
    """After a complete game, player_scores should equal the sum of per-round deltas."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        engine = await _make_engine(db_path, judge_correct=True)
        session = engine.session
        session.players = [
            PlayerState(user_id="p1", display_name="P1"),
            PlayerState(user_id="p2", display_name="P2"),
        ]
        session.state = GameState.CLUE_ACTIVE
        session.current_setter_id = "p1"
        session.remaining_setters = ["p2"]

        await engine.set_answer("蘋果汁")
        await engine.buzz_in("p2")
        result = await engine.submit_answer("p2", "蘋果汁")
        # p2 earns guesser_pts, p1 earns setter_pts

        # Advance to game over (p1 has been setter, p2 is next)
        await engine.next_round()   # transitions to SPINNING with p2 as setter

        # Simulate p2's round ends (setter timeout so we can end game quickly)
        session.state = GameState.SETTER_INPUT
        session.remaining_setters = []  # no one left after p2
        await engine.skip_setter_timeout()
        # game should be GAME_OVER now

        await asyncio.sleep(0.3)  # let all executor threads complete

        scores = _query_scores(db_path)
        # guesser_pts round 1 = 100 (correct at round 1), setter_pts = 20
        guesser_pts = result["score"]
        setter_pts = result["setter_score"]
        # p2 should have exactly guesser_pts (not double)
        # p1 should have setter_pts + SETTER_TIMEOUT_PENALTY (from timeout as p2 setter → that's p2)
        # This is complex; just verify no value is doubled
        assert scores.get("p2", 0) == guesser_pts + SETTER_TIMEOUT_PENALTY, (
            f"p2 score should be {guesser_pts + SETTER_TIMEOUT_PENALTY}, got {scores.get('p2', 0)}"
        )
        assert scores.get("p1", 0) == setter_pts, (
            f"p1 score should be {setter_pts}, got {scores.get('p1', 0)}"
        )
    finally:
        os.unlink(db_path)
