"""TDD tests for THEME_SELECT phase.

Written before implementation — all tests start red, turn green when the
engine and clue_generator changes land.

Flow being tested:
  SPINNING → begin_theme_select(themes) → THEME_SELECT
           → select_theme(theme)         → SETTER_INPUT
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from game.session import GameSession, GameState, PlayerState
from game.engine import GameEngine
from game.clue_generator import generate_clue


# ── Engine fixture ─────────────────────────────────────────────────────────────

def make_engine(on_change=None):
    session = GameSession(session_id="t1", guild_id=1, channel_id=1)
    return GameEngine(
        session,
        on_state_change=on_change or AsyncMock(),
        db_path=":memory:",
    )


async def _spin_engine(engine):
    """Bring engine from IDLE → SPINNING with two players."""
    await engine.add_player("u1", "Alice")
    await engine.add_player("u2", "Bob")
    await engine.start_game()
    assert engine.session.state == GameState.SPINNING


THEMES = ["音樂", "電影", "食物"]


# ── begin_theme_select ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_begin_theme_select_transitions_state():
    engine = make_engine()
    await _spin_engine(engine)
    await engine.begin_theme_select(THEMES)
    assert engine.session.state == GameState.THEME_SELECT


@pytest.mark.asyncio
async def test_begin_theme_select_stores_candidates():
    engine = make_engine()
    await _spin_engine(engine)
    await engine.begin_theme_select(THEMES)
    assert engine.session.candidate_themes == THEMES


@pytest.mark.asyncio
async def test_begin_theme_select_notifies():
    on_change = AsyncMock()
    engine = make_engine(on_change)
    await _spin_engine(engine)
    await engine.begin_theme_select(THEMES)
    on_change.assert_called()


@pytest.mark.asyncio
async def test_begin_theme_select_noop_if_not_spinning():
    engine = make_engine()
    await engine.add_player("u1", "Alice")
    # Still in IDLE/JOINING — not SPINNING
    await engine.begin_theme_select(THEMES)
    assert engine.session.state != GameState.THEME_SELECT


# ── select_theme ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_select_theme_transitions_to_setter_input():
    engine = make_engine()
    await _spin_engine(engine)
    await engine.begin_theme_select(THEMES)
    await engine.select_theme("音樂")
    assert engine.session.state == GameState.SETTER_INPUT


@pytest.mark.asyncio
async def test_select_theme_stores_theme():
    engine = make_engine()
    await _spin_engine(engine)
    await engine.begin_theme_select(THEMES)
    await engine.select_theme("電影")
    assert engine.session.current_theme == "電影"


@pytest.mark.asyncio
async def test_select_theme_notifies():
    on_change = AsyncMock()
    engine = make_engine(on_change)
    await _spin_engine(engine)
    on_change.reset_mock()
    await engine.begin_theme_select(THEMES)
    on_change.reset_mock()
    await engine.select_theme("音樂")
    on_change.assert_called()


@pytest.mark.asyncio
async def test_select_theme_rejects_unknown_theme():
    engine = make_engine()
    await _spin_engine(engine)
    await engine.begin_theme_select(THEMES)
    result = await engine.select_theme("宇宙")  # not in THEMES
    assert result is False
    assert engine.session.state == GameState.THEME_SELECT


@pytest.mark.asyncio
async def test_select_theme_noop_if_not_in_theme_select():
    engine = make_engine()
    await _spin_engine(engine)
    # Never called begin_theme_select — state is SPINNING
    result = await engine.select_theme("音樂")
    assert result is False
    assert engine.session.state == GameState.SPINNING


@pytest.mark.asyncio
async def test_theme_cleared_on_round_reset():
    """After a round ends and next_round() runs, current_theme resets."""
    engine = make_engine()
    await _spin_engine(engine)
    await engine.begin_theme_select(THEMES)
    await engine.select_theme("音樂")
    assert engine.session.current_theme == "音樂"

    # Advance through a full round
    engine.session.current_setter_id = "u1"
    engine.session.remaining_setters = ["u2"]
    await engine.set_answer("耳機")
    await engine.buzz_in("u2")
    judge = AsyncMock(return_value=True)
    engine._judge_fn = judge
    await engine.submit_answer("u2", "耳機")
    await engine.next_round()

    assert engine.session.current_theme is None


# ── clue_generator — theme context ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_clue_includes_theme_in_system():
    """When theme is provided, it appears in the system prompt."""
    captured = {}

    async def capture(**kwargs):
        captured.update(kwargs)
        return "ok"

    router = MagicMock()
    router.complete = capture
    await generate_clue("耳機", 1, [], router, theme="音樂")
    assert "音樂" in captured["system"]


@pytest.mark.asyncio
async def test_generate_clue_without_theme_still_works():
    """Theme is optional — omitting it must not crash or change behaviour."""
    router = MagicMock()
    router.complete = AsyncMock(return_value="神秘線索")
    clue = await generate_clue("耳機", 1, [], router)
    assert clue == "神秘線索"
