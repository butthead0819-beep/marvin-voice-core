"""
Tests for:
  1. /busted99_guess slash command — keyboard fallback for voice input
  2. Logging in receive_voice_answer_by_speaker — every exit path must log

/busted99_guess lets ANY registered player type a number when voice fails.
It shares the exact same validation path as receive_voice_answer_by_speaker
so it acts as both a UX fallback AND a debugging tool.
"""
from __future__ import annotations

import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_bot():
    bot = MagicMock()
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    return bot


def _make_interaction(user_display_name: str, user_id: int = 12345):
    interaction = MagicMock()
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.user = MagicMock()
    interaction.user.display_name = user_display_name
    interaction.user.id = user_id
    return interaction


async def _bootstrap_guessing(cog, *, jack_id: str = "jack_001", jack_name: str = "狗與露"):
    """Human (狗與露) is guesser, Marvin is setter. answer=50, range [1,99]."""
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

    await engine.add_player("marvin", "Marvin")
    await engine.add_player(jack_id, jack_name)
    cog._name_to_id[jack_name] = int(jack_id, 10) if jack_id.isdigit() else 12345

    session.setter_id = "marvin"
    session.current_guesser_id = jack_id
    session.answer = 50
    session.low_bound = 1
    session.high_bound = 99
    session.guessing_queue = []

    from game.busted99.session import Busted99State as S
    session.state = S.GUESSING

    cog._play_sfx = AsyncMock()
    return session, engine, jack_id


# ══════════════════════════════════════════════════════════════════════════════
# /busted99_guess — 基本功能
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_keyboard_guess_no_game_replies_ephemeral():
    """When no game is running, /busted99_guess must reply with an ephemeral error."""
    from cogs.busted99_cog import Busted99Cog
    cog = Busted99Cog(_make_bot())
    interaction = _make_interaction("狗與露")

    await cog.busted99_guess.callback(cog, interaction, 30)

    interaction.response.send_message.assert_called_once()
    call_kwargs = interaction.response.send_message.call_args
    assert call_kwargs.kwargs.get("ephemeral") is True, (
        "/busted99_guess must reply ephemerally when no game is running."
    )


@pytest.mark.asyncio
async def test_keyboard_guess_wrong_player_replies_ephemeral():
    """
    If the caller is not the current guesser, the command must reply ephemerally
    so only the correct player gets routing feedback.
    """
    from cogs.busted99_cog import Busted99Cog
    cog = Busted99Cog(_make_bot())
    await _bootstrap_guessing(cog, jack_id="12345", jack_name="狗與露")

    # Marvin's setter turn — Jack is guesser, so Marvin trying to guess is wrong
    interaction = _make_interaction("Marvin", user_id=99999)
    await cog.busted99_guess.callback(cog, interaction, 40)

    interaction.response.send_message.assert_called_once()
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_keyboard_guess_valid_wrong_low_sends_embed():
    """
    A valid in-range guess (wrong_low) must:
    - respond to the interaction (ephemeral ack)
    - send a guess-result embed to the channel
    """
    from cogs.busted99_cog import Busted99Cog
    cog = Busted99Cog(_make_bot())
    await _bootstrap_guessing(cog, jack_id="12345", jack_name="狗與露")

    interaction = _make_interaction("狗與露", user_id=12345)
    await cog.busted99_guess.callback(cog, interaction, 30)  # 30 < 50 → wrong_low

    interaction.response.send_message.assert_called_once()
    cog._channel.send.assert_called_once()
    call_kwargs = cog._channel.send.call_args
    assert call_kwargs.kwargs.get("embed") is not None, (
        "busted99_guess must send a guess-result embed to the channel for valid wrong guesses."
    )


@pytest.mark.asyncio
async def test_keyboard_guess_out_of_range_replies_with_feedback():
    """
    When the guess is out_of_range, /busted99_guess must send feedback
    to the channel (not silently fail).
    """
    from cogs.busted99_cog import Busted99Cog
    cog = Busted99Cog(_make_bot())
    session, engine, jack_id = await _bootstrap_guessing(
        cog, jack_id="12345", jack_name="狗與露"
    )
    session.low_bound = 20
    session.high_bound = 80

    interaction = _make_interaction("狗與露", user_id=12345)
    await cog.busted99_guess.callback(cog, interaction, 5)  # below low_bound → out_of_range

    cog._channel.send.assert_called()


@pytest.mark.asyncio
async def test_keyboard_guess_boundary_replies_with_feedback():
    """
    When the guess equals a boundary, /busted99_guess must send feedback to the channel.
    """
    from cogs.busted99_cog import Busted99Cog
    cog = Busted99Cog(_make_bot())
    session, engine, jack_id = await _bootstrap_guessing(
        cog, jack_id="12345", jack_name="狗與露"
    )

    interaction = _make_interaction("狗與露", user_id=12345)
    await cog.busted99_guess.callback(cog, interaction, 1)  # boundary

    cog._channel.send.assert_called()


# ══════════════════════════════════════════════════════════════════════════════
# Logging in receive_voice_answer_by_speaker
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_voice_answer_logs_on_wrong_speaker():
    """
    When the speaker is NOT the current guesser, a debug log must be emitted
    so operators can confirm the routing is reaching this function.
    """
    from cogs.busted99_cog import Busted99Cog
    cog = Busted99Cog(_make_bot())
    await _bootstrap_guessing(cog, jack_id="12345", jack_name="狗與露")

    with patch("cogs.busted99_cog.logger") as mock_logger:
        result = await cog.receive_voice_answer_by_speaker("Marvin", "30")

    assert result is False
    assert mock_logger.debug.called or mock_logger.info.called, (
        "A log entry must be emitted when a non-guesser's voice is rejected, "
        "so operators can confirm routing is working and identify name mismatches."
    )


@pytest.mark.asyncio
async def test_voice_answer_logs_on_parse_fail():
    """
    When parse_number fails, a debug/warning log must be emitted with the raw text.
    """
    from cogs.busted99_cog import Busted99Cog
    cog = Busted99Cog(_make_bot())
    await _bootstrap_guessing(cog, jack_id="12345", jack_name="狗與露")

    with patch("cogs.busted99_cog.logger") as mock_logger:
        result = await cog.receive_voice_answer_by_speaker("狗與露", "hello world")

    assert result is False
    assert mock_logger.debug.called or mock_logger.warning.called, (
        "A log entry must be emitted when parse_number fails so operators can "
        "see the raw STT text that couldn't be parsed."
    )


@pytest.mark.asyncio
async def test_voice_answer_logs_success():
    """
    A successful valid guess must emit an info/debug log for traceability.
    """
    from cogs.busted99_cog import Busted99Cog
    cog = Busted99Cog(_make_bot())
    await _bootstrap_guessing(cog, jack_id="12345", jack_name="狗與露")

    with patch("cogs.busted99_cog.logger") as mock_logger:
        result = await cog.receive_voice_answer_by_speaker("狗與露", "30")

    assert result is True
    assert mock_logger.info.called or mock_logger.debug.called, (
        "A log entry must be emitted on successful guess submission."
    )
