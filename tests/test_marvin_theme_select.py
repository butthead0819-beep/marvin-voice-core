"""TDD tests for Marvin auto-theme-select when Marvin is the setter.

Bug: when Marvin is drawn as setter, the game enters THEME_SELECT and
renders ThemeSelectView (timeout=150s). Marvin never clicks a button,
so the game hangs for 2.5 minutes before auto-resolving.

Fix: on_state_change(THEME_SELECT) detects setter=="marvin" and spawns
_marvin_theme_select_task instead of presenting the human view.
"""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from game.session import GameSession, GameState
from cogs.game_cog import BustedCog


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_session(setter_id: str) -> GameSession:
    s = GameSession(session_id="test", guild_id=1, channel_id=1)
    s.state = GameState.THEME_SELECT
    s.current_setter_id = setter_id
    s.candidate_themes = ["音樂", "電影", "食物"]
    return s


def _make_cog(session: GameSession):
    bot = MagicMock()
    bot.cogs.get.return_value = None
    bot.voice_clients = []
    cog = BustedCog(bot)
    cog._session = session
    engine = AsyncMock()
    engine.session = session
    cog._engine = engine
    channel = AsyncMock(spec=discord.TextChannel)
    cog._channel = channel
    cog._game_state = GameState.SPINNING  # previous state
    return cog


# ── Test: Marvin setter → _marvin_theme_select_task is spawned ─────────────────

@pytest.mark.asyncio
async def test_marvin_as_setter_spawns_auto_theme_task():
    """When setter is Marvin, on_state_change(THEME_SELECT) must spawn the auto-task."""
    session = _make_session("marvin")
    cog = _make_cog(session)

    spawned = []
    real_spawn = cog._spawn
    def tracking_spawn(coro):
        spawned.append(coro.__qualname__ if hasattr(coro, '__qualname__') else str(type(coro)))
        t = asyncio.get_running_loop().create_task(coro)
        cog._tasks.add(t)
        t.add_done_callback(cog._tasks.discard)
        return t
    cog._spawn = tracking_spawn

    with patch.object(cog, '_post_game_message', new_callable=AsyncMock):
        await cog.on_state_change(session)

    # The auto-task name should contain "marvin_theme"
    assert any("marvin_theme" in name for name in spawned), \
        f"Expected marvin_theme_select_task to be spawned, got: {spawned}"


@pytest.mark.asyncio
async def test_human_setter_shows_theme_select_view():
    """When setter is a human, ThemeSelectView should be posted (no auto-task)."""
    session = _make_session("u1")
    cog = _make_cog(session)

    spawned = []
    def tracking_spawn(coro):
        spawned.append(coro.__qualname__ if hasattr(coro, '__qualname__') else str(type(coro)))
        t = asyncio.get_running_loop().create_task(coro)
        cog._tasks.add(t)
        t.add_done_callback(cog._tasks.discard)
        return t
    cog._spawn = tracking_spawn

    posted_views = []
    async def capture_post(embed, view=None):
        posted_views.append(view)
        return MagicMock(id=999)
    cog._post_game_message = capture_post

    await cog.on_state_change(session)

    # Should NOT spawn a marvin_theme task
    assert not any("marvin_theme" in name for name in spawned), \
        "Human setter should not spawn marvin_theme_select_task"
    # Should post a ThemeSelectView
    from cogs.game_cog import ThemeSelectView
    assert any(isinstance(v, ThemeSelectView) for v in posted_views), \
        "Human setter should see ThemeSelectView"


# ── Test: _marvin_theme_select_task picks and calls select_theme ───────────────

@pytest.mark.asyncio
async def test_marvin_theme_select_task_calls_select_theme():
    """_marvin_theme_select_task must call engine.select_theme with a valid candidate."""
    session = _make_session("marvin")
    cog = _make_cog(session)

    selected = []
    async def mock_select(theme):
        selected.append(theme)
        session.state = GameState.SETTER_INPUT
        return True
    cog._engine.select_theme = mock_select

    with patch('asyncio.sleep', new_callable=AsyncMock):
        await cog._marvin_theme_select_task()

    assert len(selected) == 1
    assert selected[0] in session.candidate_themes, \
        f"Marvin must pick from candidates, got: {selected[0]}"


@pytest.mark.asyncio
async def test_marvin_theme_select_task_noop_if_state_changed():
    """If state is no longer THEME_SELECT when task runs, it should be a no-op."""
    session = _make_session("marvin")
    session.state = GameState.SETTER_INPUT  # already moved on
    cog = _make_cog(session)

    with patch('asyncio.sleep', new_callable=AsyncMock):
        await cog._marvin_theme_select_task()

    cog._engine.select_theme.assert_not_called()


@pytest.mark.asyncio
async def test_marvin_theme_select_task_noop_if_no_candidates():
    """If there are no candidate themes (edge case), task does nothing."""
    session = _make_session("marvin")
    session.candidate_themes = []
    cog = _make_cog(session)

    with patch('asyncio.sleep', new_callable=AsyncMock):
        await cog._marvin_theme_select_task()

    cog._engine.select_theme.assert_not_called()
