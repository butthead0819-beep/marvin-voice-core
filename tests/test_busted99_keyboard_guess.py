"""
Tests for:
  1. on_message chat-number routing — keyboard fallback for voice input
     When the current guesser types a number in chat, treat it as a guess.
  2. ConversationBuffer.game_mode_cap — VAD silence threshold capped during game
     High-temperature sessions normally require 3.0s silence before cutting audio;
     in game mode the cap reduces this to ≤0.8s so short number utterances
     are processed within 0.8s of silence rather than 3.0s.
  3. Logging in receive_voice_answer_by_speaker — every exit path must log
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


def _make_message(content: str, display_name: str, user_id: int = 12345,
                  channel=None):
    msg = MagicMock()
    msg.content = content
    msg.author = MagicMock()
    msg.author.display_name = display_name
    msg.author.id = user_id
    msg.author.bot = False
    msg.channel = channel or MagicMock()
    msg.delete = AsyncMock()
    return msg


async def _bootstrap_guessing(cog, *, jack_id: str = "12345",
                               jack_name: str = "狗與露"):
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
    cog._name_to_id[jack_name] = int(jack_id)

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
# on_message — chat number routing
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_on_message_number_routes_as_guess():
    """
    When the current guesser sends a message that parse_number can extract
    a number from, it must be treated as a guess and the channel embed is sent.
    """
    from cogs.busted99_cog import Busted99Cog
    cog = Busted99Cog(_make_bot())
    await _bootstrap_guessing(cog)

    msg = _make_message("30", "狗與露")
    await cog.on_message(msg)

    assert cog._channel.send.call_count >= 1, "channel.send must be called at least once"
    # First call is the result embed; subsequent calls may re-post the guessing embed.
    first_call = cog._channel.send.call_args_list[0]
    assert first_call.kwargs.get("embed") is not None, (
        "A number typed in chat by the current guesser must route as a guess "
        "and produce a result embed."
    )


@pytest.mark.asyncio
async def test_on_message_non_number_ignored():
    """
    When the current guesser sends a non-numeric message, it must be ignored
    (no _process_guess, no channel.send from the game cog).
    """
    from cogs.busted99_cog import Busted99Cog
    cog = Busted99Cog(_make_bot())
    await _bootstrap_guessing(cog)

    msg = _make_message("hello world", "狗與露")
    await cog.on_message(msg)

    cog._channel.send.assert_not_called(), (
        "Non-numeric messages from the guesser must be ignored — "
        "only numeric inputs should route as guesses."
    )


@pytest.mark.asyncio
async def test_on_message_from_non_guesser_ignored():
    """
    When a player who is NOT the current guesser types a number, it must be ignored.
    """
    from cogs.busted99_cog import Busted99Cog
    cog = Busted99Cog(_make_bot())
    await _bootstrap_guessing(cog)

    # Marvin is setter (not guesser); give him a distinct user_id so he isn't mistaken for jack
    msg = _make_message("40", "Marvin", user_id=99999)
    await cog.on_message(msg)

    cog._channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_on_message_no_game_ignored():
    """When no game is running, on_message must do nothing."""
    from cogs.busted99_cog import Busted99Cog
    cog = Busted99Cog(_make_bot())
    # _engine and _session are None by default

    msg = _make_message("30", "狗與露")
    await cog.on_message(msg)  # must not raise


@pytest.mark.asyncio
async def test_on_message_bot_message_ignored():
    """Bot messages must always be ignored."""
    from cogs.busted99_cog import Busted99Cog
    cog = Busted99Cog(_make_bot())
    await _bootstrap_guessing(cog)

    msg = _make_message("30", "狗與露")
    msg.author.bot = True
    await cog.on_message(msg)

    cog._channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_on_message_out_of_range_sends_feedback():
    """
    When the guesser types an out-of-range number, the channel must
    receive a feedback message (not silently fail).
    """
    from cogs.busted99_cog import Busted99Cog
    cog = Busted99Cog(_make_bot())
    session, _, _ = await _bootstrap_guessing(cog)
    session.low_bound = 20
    session.high_bound = 80

    msg = _make_message("5", "狗與露")  # out of [20, 80]
    await cog.on_message(msg)

    cog._channel.send.assert_called()


# ══════════════════════════════════════════════════════════════════════════════
# ConversationBuffer.game_mode_cap — VAD threshold cap
# ══════════════════════════════════════════════════════════════════════════════

def test_conversation_buffer_game_mode_cap_limits_threshold():
    """
    In high-temperature sessions (>8 utterances → 3.0s), setting game_mode_cap
    must reduce the returned threshold to at most the cap value (0.8s).

    Root cause of voice failure: during active gameplay, conv temperature is HIGH
    (3.0s silence needed), so short number utterances (0.3-0.5s of speech) are
    never cut and never sent to STT. Cap fixes this.
    """
    from discord_voice_engine import ConversationBuffer
    import time

    buf = ConversationBuffer()
    # Simulate high-temperature session: 10 utterances in the last 60 seconds
    now = time.time()
    for i in range(10):
        buf.history.append({"timestamp": now - i * 3, "speaker": "A", "text": f"utt{i}"})

    # Without cap: should be 3.0s
    assert buf.get_conversation_temperature() == 3.0

    # With game_mode_cap=0.8: must be ≤ 0.8
    buf.game_mode_cap = 0.8
    assert buf.get_conversation_temperature() <= 0.8, (
        "game_mode_cap must limit the VAD silence threshold so short number "
        "utterances (~0.3s) are processed quickly during game mode."
    )


def test_conversation_buffer_game_mode_cap_none_restores_normal():
    """
    After resetting game_mode_cap to None, the threshold reverts to normal.
    """
    from discord_voice_engine import ConversationBuffer
    import time

    buf = ConversationBuffer()
    now = time.time()
    for i in range(10):
        buf.history.append({"timestamp": now - i * 3, "speaker": "A", "text": f"utt{i}"})

    buf.game_mode_cap = 0.8
    assert buf.get_conversation_temperature() <= 0.8

    buf.game_mode_cap = None
    assert buf.get_conversation_temperature() == 3.0, (
        "Resetting game_mode_cap must restore normal temperature behaviour."
    )


def test_conversation_buffer_cap_not_raised_above_natural():
    """
    When the natural temperature is already LOW (0.8s) and cap is 0.8,
    the result must still be 0.8 (cap doesn't raise values, only lowers them).
    """
    from discord_voice_engine import ConversationBuffer

    buf = ConversationBuffer()
    # Empty history → natural temperature = 0.8s (low)
    buf.game_mode_cap = 0.8
    assert buf.get_conversation_temperature() == 0.8


# ══════════════════════════════════════════════════════════════════════════════
# Logging in receive_voice_answer_by_speaker
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_voice_answer_logs_on_wrong_speaker():
    """
    When the speaker is NOT the current guesser, a debug log must be emitted.
    """
    from cogs.busted99_cog import Busted99Cog
    cog = Busted99Cog(_make_bot())
    await _bootstrap_guessing(cog)

    with patch("cogs.busted99_cog.logger") as mock_logger:
        result = await cog.receive_voice_answer_by_speaker("Marvin", "30")

    assert result is False
    assert mock_logger.debug.called or mock_logger.info.called


@pytest.mark.asyncio
async def test_voice_answer_logs_on_parse_fail():
    """
    When parse_number fails, a debug/warning log must be emitted with raw text.
    """
    from cogs.busted99_cog import Busted99Cog
    cog = Busted99Cog(_make_bot())
    await _bootstrap_guessing(cog)

    with patch("cogs.busted99_cog.logger") as mock_logger:
        result = await cog.receive_voice_answer_by_speaker("狗與露", "hello world")

    assert result is False
    assert mock_logger.debug.called or mock_logger.warning.called


@pytest.mark.asyncio
async def test_voice_answer_logs_success():
    """
    A successful valid guess must emit an info/debug log.
    """
    from cogs.busted99_cog import Busted99Cog
    cog = Busted99Cog(_make_bot())
    await _bootstrap_guessing(cog)

    with patch("cogs.busted99_cog.logger") as mock_logger:
        result = await cog.receive_voice_answer_by_speaker("狗與露", "30")

    assert result is True
    assert mock_logger.info.called or mock_logger.debug.called
