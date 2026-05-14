"""Tests for /busted_stop — force-reset a stuck or active Busted game."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from game.session import GameSession, GameState


def _make_bot(*, has_vc: bool = False) -> MagicMock:
    bot = MagicMock()
    bot.voice_clients = []
    if has_vc:
        vc = MagicMock()
        vc.game_mode = True
        bot.cogs.get.return_value = vc
    else:
        bot.cogs.get.return_value = None
    return bot


def _make_cog(bot):
    with patch("cogs.game_cog.MemoryManager"):
        from cogs.game_cog import BustedCog
        cog = BustedCog(bot)
    return cog


def _make_interaction() -> MagicMock:
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


def _inject_active_game(cog):
    """Simulate a game that started but is stuck (e.g. JOINING state)."""
    session = GameSession(session_id="stuck1", guild_id=1, channel_id=1)
    cog._session = session
    cog._engine = MagicMock()
    cog._game_state = GameState.JOINING
    cog._channel = MagicMock()
    return session


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_busted_stop_when_no_game_running():
    """/busted_stop with no active game replies with an ephemeral 'no game' message."""
    bot = _make_bot()
    cog = _make_cog(bot)
    interaction = _make_interaction()

    await cog.busted_stop.callback(cog, interaction)

    interaction.response.send_message.assert_called_once()
    msg = interaction.response.send_message.call_args
    assert msg.kwargs.get("ephemeral") is True
    text = msg.args[0] if msg.args else ""
    assert "沒有" in text or "no game" in text.lower()


@pytest.mark.asyncio
async def test_busted_stop_clears_engine_and_session():
    """/busted_stop clears _engine and _session so a new game can be started."""
    bot = _make_bot()
    cog = _make_cog(bot)
    _inject_active_game(cog)
    interaction = _make_interaction()

    await cog.busted_stop.callback(cog, interaction)

    assert cog._engine is None
    assert cog._session is None
    assert cog._game_state is None


@pytest.mark.asyncio
async def test_busted_stop_clears_name_to_id_and_grace_timers():
    """/busted_stop cleans up internal bookkeeping dicts."""
    bot = _make_bot()
    cog = _make_cog(bot)
    _inject_active_game(cog)
    cog._name_to_id["Alice"] = 123
    fake_timer = MagicMock()
    cog._grace_timers["456"] = fake_timer
    interaction = _make_interaction()

    await cog.busted_stop.callback(cog, interaction)

    assert cog._name_to_id == {}
    assert cog._grace_timers == {}
    fake_timer.cancel.assert_called_once()


@pytest.mark.asyncio
async def test_busted_stop_restores_vc_game_mode():
    """/busted_stop sets vc.game_mode = False when VoiceController is present."""
    bot = _make_bot(has_vc=True)
    vc = bot.cogs.get.return_value
    cog = _make_cog(bot)
    _inject_active_game(cog)
    interaction = _make_interaction()

    await cog.busted_stop.callback(cog, interaction)

    assert vc.game_mode is False


@pytest.mark.asyncio
async def test_busted_stop_no_vc_does_not_crash():
    """/busted_stop works even when VoiceController cog is absent."""
    bot = _make_bot(has_vc=False)
    cog = _make_cog(bot)
    _inject_active_game(cog)
    interaction = _make_interaction()

    await cog.busted_stop.callback(cog, interaction)  # must not raise

    assert cog._engine is None


@pytest.mark.asyncio
async def test_busted_stop_allows_restart():
    """After /busted_stop, /busted_start should no longer see 'already running'."""
    bot = _make_bot()
    cog = _make_cog(bot)
    _inject_active_game(cog)
    interaction = _make_interaction()

    await cog.busted_stop.callback(cog, interaction)

    # Engine is cleared — busted_start check passes
    assert cog._engine is None
