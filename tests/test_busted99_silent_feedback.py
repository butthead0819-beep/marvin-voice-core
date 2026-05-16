"""
Tests for on_message keyboard input path in Busted99Cog.

All guess input now goes through on_message → _process_guess (voice input disabled).
Key behaviors:
  1. Non-number text from the guesser → silently ignored (might be normal chat)
  2. Message from non-guesser → silently ignored
  3. Out-of-range number → _channel.send with range hint
  4. Boundary number → _channel.send with boundary warning
  5. Valid in-range non-boundary number → embed sent with guess result
"""
from __future__ import annotations

import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_bot():
    bot = MagicMock()
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    return bot


def _make_cog(bot=None):
    if bot is None:
        bot = _make_bot()
    from cogs.busted99_cog import Busted99Cog
    cog = Busted99Cog(bot)
    return cog


def _make_message(author_id: str, content: str, is_bot: bool = False) -> MagicMock:
    msg = MagicMock()
    msg.author.bot = is_bot
    msg.author.id = author_id
    msg.author.display_name = "狗與露"
    msg.content = content
    return msg


async def _bootstrap_guessing_human(cog):
    """Human (狗與露) is the guesser, Marvin is the setter."""
    from game.busted99.engine import Busted99Engine
    from game.busted99.session import Busted99Session, Busted99State

    session = Busted99Session(
        session_id=str(uuid.uuid4()),
        guild_id=1,
        channel_id=1,
    )
    cog._channel = AsyncMock()
    cog._channel.send = AsyncMock()

    async def _noop_state(s):
        pass

    engine = Busted99Engine(
        session=session,
        on_state_change=_noop_state,
        db_path=":memory:",
    )
    cog._engine = engine
    cog._session = session

    jack_id = "jack_001"
    await engine.add_player("marvin", "Marvin")
    await engine.add_player(jack_id, "狗與露")

    session.setter_id = "marvin"
    session.current_guesser_id = jack_id
    session.answer = 50
    session.low_bound = 1
    session.high_bound = 99
    session.guessing_queue = []
    session.state = Busted99State.GUESSING

    return session, engine, jack_id


# ══════════════════════════════════════════════════════════════════════════════
# Non-number input → silently ignored
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_on_message_ignores_non_number():
    """
    When the guesser sends non-number text, on_message silently ignores it.
    Normal chat must not trigger any guess feedback.
    """
    cog = _make_cog()
    await _bootstrap_guessing_human(cog)

    msg = _make_message("jack_001", "hello world")
    await cog.on_message(msg)

    cog._channel.send.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# Non-guesser message → silently ignored
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_on_message_ignores_non_guesser():
    """
    When someone other than the current guesser sends a number,
    on_message silently ignores it — no channel.send, no guess processed.
    """
    cog = _make_cog()
    await _bootstrap_guessing_human(cog)

    msg = _make_message("other_user_999", "30")
    await cog.on_message(msg)

    cog._channel.send.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# Out-of-range → feedback required
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_on_message_out_of_range_sends_channel_message():
    """
    When the guesser types a number outside [low_bound, high_bound],
    _channel.send() must be called with the valid range hint.
    """
    cog = _make_cog()
    session, engine, jack_id = await _bootstrap_guessing_human(cog)

    session.low_bound = 40
    session.high_bound = 60

    msg = _make_message(jack_id, "10")  # below low_bound=40
    await cog.on_message(msg)

    cog._channel.send.assert_called_once()
    call_text = cog._channel.send.call_args[0][0]
    assert "超出範圍" in call_text or "40" in call_text


# ══════════════════════════════════════════════════════════════════════════════
# Boundary guess → feedback required
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_on_message_boundary_sends_channel_message():
    """
    When the guesser types the boundary value (low_bound or high_bound),
    _channel.send() must be called with the boundary warning.
    """
    cog = _make_cog()
    session, engine, jack_id = await _bootstrap_guessing_human(cog)

    session.low_bound = 1
    session.high_bound = 99

    msg = _make_message(jack_id, "1")  # equals low_bound → boundary
    await cog.on_message(msg)

    cog._channel.send.assert_called_once()
    call_text = cog._channel.send.call_args[0][0]
    assert "邊界" in call_text or "1" in call_text


# ══════════════════════════════════════════════════════════════════════════════
# Valid wrong guess → embed sent
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_on_message_valid_wrong_sends_embed():
    """
    A valid in-range non-boundary wrong guess must send an embed with the
    guess result. This is the happy path for the guesser experience.
    """
    cog = _make_cog()
    session, engine, jack_id = await _bootstrap_guessing_human(cog)
    cog._play_sfx = AsyncMock()

    msg = _make_message(jack_id, "30")  # valid: 1 < 30 < 99, not boundary
    await cog.on_message(msg)

    assert cog._channel.send.call_count >= 1
    first_call = cog._channel.send.call_args_list[0]
    assert first_call.kwargs.get("embed") is not None or (
        first_call.args and hasattr(first_call.args[0], "title")
    ), "Valid wrong guess must send an embed, not plain text."
