"""
TDD for Detective (謊言偵探) immediate score persistence.
Scores must be written to player_scores after each round's close_voting,
not only at game end.

Score change events:
1. close_voting: correct voters get GUESSER_CORRECT_SCORE, declarer gets per-fool pts
2. Full multi-round game: no double-count
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile

import pytest
from unittest.mock import AsyncMock

from game.detective.session import (
    DetectiveSession, DetectiveState, PlayerDState, StatementSet,
)
from game.detective.engine import DetectiveEngine, GUESSER_CORRECT_SCORE, DECLARER_PER_FOOL_SCORE


# ── helpers ───────────────────────────────────────────────────────────────────

async def _make_engine(db_path: str) -> DetectiveEngine:
    engine = DetectiveEngine(
        DetectiveSession(session_id="t1", guild_id=1, channel_id=1),
        on_state_change=AsyncMock(),
        db_path=db_path,
    )
    await asyncio.sleep(0.05)
    return engine


def _query_scores(db_path: str) -> dict[str, int]:
    con = sqlite3.connect(db_path)
    rows = con.execute("SELECT user_id, score FROM player_scores").fetchall()
    con.close()
    return dict(rows)


def _setup_voting(engine: DetectiveEngine,
                  declarer_id: str = "p1",
                  correct_voter_ids: list[str] = None,
                  fooled_voter_ids: list[str] = None,
                  lie_index: int = 2) -> None:
    """Put session directly into VOTING state with votes cast."""
    correct_voter_ids = correct_voter_ids or ["p2"]
    fooled_voter_ids = fooled_voter_ids or ["p3"]
    s = engine.session
    all_ids = [declarer_id] + correct_voter_ids + fooled_voter_ids
    s.players = [PlayerDState(user_id=uid, display_name=uid.upper()) for uid in all_ids]
    s.current_declarer_id = declarer_id
    s.current_statements = StatementSet(a="真A", b="真B", c="謊C", lie_index=lie_index)
    s.state = DetectiveState.VOTING
    for p in s.players:
        if p.user_id in correct_voter_ids:
            p.vote = lie_index      # correct
        elif p.user_id in fooled_voter_ids:
            p.vote = (lie_index + 1) % 3  # wrong


# ── 1. close_voting persists scores immediately ───────────────────────────────

@pytest.mark.asyncio
async def test_correct_voter_score_persisted_after_close_voting():
    """Correct voter's score should appear in player_scores right after close_voting."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        engine = await _make_engine(db_path)
        _setup_voting(engine, declarer_id="p1",
                      correct_voter_ids=["p2"], fooled_voter_ids=["p3"])
        engine.session.declarer_queue = []  # will end game after this round

        await engine.close_voting()

        await asyncio.sleep(0.15)

        scores = _query_scores(db_path)
        assert scores.get("p2", 0) == GUESSER_CORRECT_SCORE, (
            f"correct voter should have {GUESSER_CORRECT_SCORE}, got {scores.get('p2', 0)}"
        )
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_declarer_score_persisted_after_close_voting():
    """Declarer's per-fool score should appear in player_scores right after close_voting."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        engine = await _make_engine(db_path)
        # 2 fooled voters → declarer gets 2 * DECLARER_PER_FOOL_SCORE
        _setup_voting(engine, declarer_id="p1",
                      correct_voter_ids=["p2"], fooled_voter_ids=["p3", "p4"])
        engine.session.declarer_queue = []

        await engine.close_voting()

        await asyncio.sleep(0.15)

        scores = _query_scores(db_path)
        expected = 2 * DECLARER_PER_FOOL_SCORE
        assert scores.get("p1", 0) == expected, (
            f"declarer should have {expected}, got {scores.get('p1', 0)}"
        )
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_scores_persisted_before_game_ends():
    """Scores should be in DB after first round even if game has more rounds remaining."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        engine = await _make_engine(db_path)
        _setup_voting(engine, declarer_id="p1",
                      correct_voter_ids=["p2"], fooled_voter_ids=["p3"])
        # Another round remains (p2 is still in queue)
        engine.session.declarer_queue = ["p2"]

        result = await engine.close_voting()

        # Game NOT over yet (still has p2 in queue), but scores should already be persisted
        await asyncio.sleep(0.15)

        scores = _query_scores(db_path)
        assert scores.get("p2", 0) == GUESSER_CORRECT_SCORE, (
            "p2's score should be persisted even though game isn't over yet"
        )
    finally:
        os.unlink(db_path)


# ── 2. No double-count after full game ────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_double_count_detective():
    """After full game, player_scores equals sum of per-round deltas, not 2x."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        engine = await _make_engine(db_path)

        # Round 1: p1 declares, p2 correct, p3 fooled
        _setup_voting(engine, declarer_id="p1",
                      correct_voter_ids=["p2"], fooled_voter_ids=["p3"])
        engine.session.declarer_queue = ["p2"]  # p2 still has a round left

        await engine.close_voting()
        await asyncio.sleep(0.05)

        # Round 2: set up p2 as declarer, advance
        engine.session.state = DetectiveState.VOTING
        engine.session.current_declarer_id = "p2"
        engine.session.current_statements = StatementSet(a="真A", b="真B", c="謊C", lie_index=2)
        engine.session.declarer_queue = []  # game over after this
        for p in engine.session.players:
            if p.user_id == "p1":
                p.vote = 2  # correct
            elif p.user_id == "p3":
                p.vote = 0  # fooled

        await engine.close_voting()
        await asyncio.sleep(0.2)

        scores = _query_scores(db_path)
        # Round 1: p1 declares (fooled p3 → +30), p2 correct voter (+50), p3 fooled (0)
        # Round 2: p2 declares (fooled p3 → +30), p1 correct voter (+50), p3 fooled (0)
        # p1 total: 30 + 50 = 80
        # p2 total: 50 + 30 = 80
        # p3 total: 0
        expected_p1 = DECLARER_PER_FOOL_SCORE + GUESSER_CORRECT_SCORE  # 80
        expected_p2 = GUESSER_CORRECT_SCORE + DECLARER_PER_FOOL_SCORE  # 80
        assert scores.get("p1", 0) == expected_p1, (
            f"p1 double-count? expected {expected_p1}, got {scores.get('p1', 0)}"
        )
        assert scores.get("p2", 0) == expected_p2, (
            f"p2 double-count? expected {expected_p2}, got {scores.get('p2', 0)}"
        )
        assert scores.get("p3", 0) == 0, f"p3 fooled both rounds, expected 0, got {scores.get('p3', 0)}"
    finally:
        os.unlink(db_path)
