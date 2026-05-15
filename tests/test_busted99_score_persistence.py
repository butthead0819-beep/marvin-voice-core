"""
TDD for Busted99 immediate score persistence.
Scores must be written to player_scores on every score change,
not only at game end.

Score change events:
1. submit_guess → bust (game over): everyone except guesser gets pts
2. submit_guess → last_wrong (game over): guesser gets 100
3. timeout_guesser (mid-game): guesser loses deduction
4. Full game: no double-count
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile

import pytest
from unittest.mock import AsyncMock

from game.busted99.session import Busted99Session, Busted99State, Player99State
from game.busted99.engine import Busted99Engine
from game.busted99.scoring import score_for_space


# ── helpers ───────────────────────────────────────────────────────────────────

async def _make_engine(db_path: str) -> Busted99Engine:
    engine = Busted99Engine(
        Busted99Session(session_id="t1", guild_id=1, channel_id=1),
        on_state_change=AsyncMock(),
        db_path=db_path,
    )
    await asyncio.sleep(0.05)  # let _init_db executor complete
    return engine


def _query_scores(db_path: str) -> dict[str, int]:
    con = sqlite3.connect(db_path)
    rows = con.execute("SELECT user_id, score FROM player_scores").fetchall()
    con.close()
    return dict(rows)


def _guessing_session(engine: Busted99Engine,
                      answer: int = 42,
                      low: int = 1,
                      high: int = 99) -> None:
    """Set session directly to GUESSING state with setter + 2 guessers."""
    s = engine.session
    s.players = [
        Player99State(user_id="setter", display_name="Setter"),
        Player99State(user_id="g1", display_name="G1"),
        Player99State(user_id="g2", display_name="G2"),
    ]
    s.setter_id = "setter"
    s.answer = answer
    s.low_bound = low
    s.high_bound = high
    s.state = Busted99State.GUESSING
    s.current_guesser_id = "g1"
    s.guessing_queue = ["g2"]


# ── 1. bust persists immediately ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bust_persists_all_scores_immediately():
    """After a bust (game over), player_scores has scores before any explicit end call."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        engine = await _make_engine(db_path)
        _guessing_session(engine, answer=42, low=1, high=99)

        result = await engine.submit_guess("g1", 42)  # bust, space=99

        assert result["result"] == "bust"
        await asyncio.sleep(0.15)

        scores = _query_scores(db_path)
        expected_pts = score_for_space(99)  # = 10
        # setter and g2 each get expected_pts; g1 (guesser) gets 0
        assert scores.get("setter", 0) == expected_pts, (
            f"setter should have {expected_pts}, got {scores.get('setter', 0)}"
        )
        assert scores.get("g2", 0) == expected_pts, (
            f"g2 should have {expected_pts}, got {scores.get('g2', 0)}"
        )
        assert scores.get("g1", 0) == 0, "guesser gets 0 on bust"
    finally:
        os.unlink(db_path)


# ── 2. last_wrong persists immediately ────────────────────────────────────────

@pytest.mark.asyncio
async def test_last_wrong_persists_guesser_score_immediately():
    """After last_wrong (game over), guesser's 100 pts is in player_scores."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        engine = await _make_engine(db_path)
        # space=2 → is_last_chance; answer=43, guess=42 → wrong (too low) → last_wrong
        _guessing_session(engine, answer=43, low=42, high=43)

        result = await engine.submit_guess("g1", 42)

        assert result["result"] == "last_wrong", f"expected last_wrong, got {result['result']}"
        await asyncio.sleep(0.15)

        scores = _query_scores(db_path)
        assert scores.get("g1", 0) == 100, (
            f"guesser should have 100 on last_wrong, got {scores.get('g1', 0)}"
        )
    finally:
        os.unlink(db_path)


# ── 3. timeout_guesser persists deduction immediately ─────────────────────────

@pytest.mark.asyncio
async def test_timeout_guesser_persists_deduction_immediately():
    """Timeout deduction should appear in player_scores before game ends."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        engine = await _make_engine(db_path)
        _guessing_session(engine, answer=42, low=1, high=99)
        # Give g1 some score first so deduction actually applies
        engine.session.players[1].score = 50  # g1 starts with 50

        await engine.timeout_guesser()

        await asyncio.sleep(0.15)

        scores = _query_scores(db_path)
        deduction = score_for_space(99)  # = 10
        expected = 50 - deduction  # = 40, delta written = -10
        assert "g1" in scores, "g1 should appear in player_scores after timeout"
        assert scores["g1"] == -deduction, (
            f"delta should be -{deduction}, got {scores['g1']}"
        )
    finally:
        os.unlink(db_path)


# ── 4. No double-count at game end ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_double_count_busted99():
    """After bust game-over, player_scores equals per-event deltas, not 2x."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        engine = await _make_engine(db_path)
        _guessing_session(engine, answer=42, low=1, high=99)

        await engine.submit_guess("g1", 42)  # bust, space=99 → everyone except g1 gets 10

        await asyncio.sleep(0.15)

        scores = _query_scores(db_path)
        # setter and g2 should each have exactly 10 (not 20 from double-count)
        assert scores.get("setter", 0) == 10, (
            f"setter double-count? expected 10, got {scores.get('setter', 0)}"
        )
        assert scores.get("g2", 0) == 10, (
            f"g2 double-count? expected 10, got {scores.get('g2', 0)}"
        )
    finally:
        os.unlink(db_path)
