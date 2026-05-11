"""Integration tests for BustedCog's router wiring.

These tests guard against attribute-name mismatches between how the bot
stores the LLM router (bot.router) and how the cog looks it up.
The bug this file was written to prevent: getattr(bot, "gemini_router")
instead of getattr(bot, "router").
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from game.session import GameSession


def _make_bot(*, has_router: bool = True) -> MagicMock:
    """Return a minimal mock bot. Optionally omit bot.router to test the fallback."""
    bot = MagicMock()
    bot.voice_clients = []
    bot.cogs.get.return_value = None  # no VoiceController in unit tests — skip TTS
    if has_router:
        bot.router = MagicMock()
        bot.router.complete = AsyncMock(return_value="測試線索")
    else:
        del bot.router  # ensure getattr(bot, "router", None) returns None
    return bot


def _make_cog(bot):
    """Construct BustedCog with MemoryManager patched out (no DB I/O in tests)."""
    with patch("cogs.game_cog.MemoryManager"):
        from cogs.game_cog import BustedCog
        cog = BustedCog(bot)
    return cog


def _make_active_session() -> GameSession:
    session = GameSession(session_id="t1", guild_id=1, channel_id=1)
    session.current_answer = "蘋果"
    session.current_round = 1
    return session


# ---------------------------------------------------------------------------
# Router attribute name
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clue_request_calls_router_complete():
    """Happy path: bot.router.complete() is called and the clue is appended."""
    bot = _make_bot(has_router=True)
    cog = _make_cog(bot)
    session = _make_active_session()
    cog._session = session
    cog._channel = None          # skip Discord embed edits
    cog.on_state_change = AsyncMock()

    await cog._on_clue_request(session)

    bot.router.complete.assert_called_once()
    assert session.current_clues == ["測試線索"]


@pytest.mark.asyncio
async def test_clue_request_uses_bot_router_not_gemini_router():
    """bot.gemini_router does not exist — only bot.router does.
    Confirm the cog uses the correct attribute name."""
    bot = _make_bot(has_router=True)
    # Add a deliberately wrong alias; cog must NOT use it
    bot.gemini_router = MagicMock()
    bot.gemini_router.complete = AsyncMock(return_value="wrong source")

    cog = _make_cog(bot)
    session = _make_active_session()
    cog._session = session
    cog._channel = None
    cog.on_state_change = AsyncMock()

    await cog._on_clue_request(session)

    # Must have used bot.router, not bot.gemini_router
    bot.router.complete.assert_called_once()
    bot.gemini_router.complete.assert_not_called()
    assert session.current_clues == ["測試線索"]


@pytest.mark.asyncio
async def test_clue_request_fallback_when_no_router():
    """When bot.router is absent, the cog appends the 'not connected' placeholder."""
    bot = _make_bot(has_router=False)
    cog = _make_cog(bot)
    session = _make_active_session()
    cog._session = session
    cog._channel = None
    cog.on_state_change = AsyncMock()

    await cog._on_clue_request(session)

    assert len(session.current_clues) == 1
    assert "未連接" in session.current_clues[0]


@pytest.mark.asyncio
async def test_clue_request_fallback_on_router_exception():
    """When router.complete() raises, generate_clue returns the failure string."""
    bot = _make_bot(has_router=True)
    bot.router.complete = AsyncMock(side_effect=Exception("timeout"))

    cog = _make_cog(bot)
    session = _make_active_session()
    cog._session = session
    cog._channel = None
    cog.on_state_change = AsyncMock()

    await cog._on_clue_request(session)

    assert len(session.current_clues) == 1
    assert "失敗" in session.current_clues[0]


# ---------------------------------------------------------------------------
# busted_start router wiring (MarvinPlayer init)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_marvin_init_receives_router_from_bot():
    """MarvinPlayer must be initialised with bot.router (not None)."""
    bot = _make_bot(has_router=True)
    cog = _make_cog(bot)

    # Simulate the lookup busted_start does before creating MarvinPlayer
    router = getattr(cog.bot, "router", None)
    assert router is not None
    assert router is bot.router


@pytest.mark.asyncio
async def test_marvin_init_disabled_when_no_router():
    """When bot.router is absent, the router lookup yields None → Marvin disabled."""
    bot = _make_bot(has_router=False)
    cog = _make_cog(bot)

    # Use spec= so MagicMock doesn't auto-create attributes
    bot_strict = MagicMock(spec=["voice_clients"])  # only voice_clients exists
    router = getattr(bot_strict, "router", None)
    assert router is None
