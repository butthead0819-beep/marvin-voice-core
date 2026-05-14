"""
Tests for silent-feedback bug in receive_voice_answer_by_speaker.

When game_mode=True, vc.play_tts() is immediately blocked by the guard in
VoiceController.play_tts (line 4063).  The three failure paths that only
call play_tts produce ZERO user feedback:

  1. parse_number fails  → plays TTS "再說一次"    (blocked) → nothing
  2. submit_guess → "out_of_range" → TTS "超出範圍" (blocked) → nothing
  3. submit_guess → "boundary"    → TTS "不可以猜邊界" (blocked) → nothing

Fix: replace / supplement blocked TTS with _channel.send() text messages
so the guesser receives feedback regardless of TTS availability.
"""
from __future__ import annotations

import asyncio
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


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

    from game.busted99.session import Busted99State as S
    session.state = S.GUESSING

    return session, engine, jack_id


# ══════════════════════════════════════════════════════════════════════════════
# Bug: parse_number fails → no feedback to user
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_parse_fail_sends_channel_message():
    """
    When the user says something that parse_number cannot extract a number from,
    _channel.send() must be called with a user-visible error message.

    Before fix: only play_tts("再說一次") is called — which is silently blocked
    by game_mode guard — so the user receives no feedback at all.
    """
    cog = _make_cog()
    session, engine, jack_id = await _bootstrap_guessing_human(cog)

    # Simulate game_mode: VoiceController.play_tts is available but returns
    # immediately (game_mode guard) — i.e., it does nothing.
    mock_vc = MagicMock()
    mock_vc.play_tts = AsyncMock(return_value=None)   # no-op (blocked)
    cog.bot.cogs.get.return_value = mock_vc

    consumed = await cog.receive_voice_answer_by_speaker("狗與露", "hello world")

    assert consumed is False
    cog._channel.send.assert_called_once(), (
        "Silent-feedback bug: _channel.send must be called when parse_number fails, "
        "so the user sees feedback even when play_tts is blocked by game_mode."
    )


@pytest.mark.asyncio
async def test_parse_fail_no_vc_still_sends_channel_message():
    """
    Even when VoiceController cog is absent (vc is None),
    _channel.send() must still be called.
    """
    cog = _make_cog()
    session, engine, jack_id = await _bootstrap_guessing_human(cog)
    cog.bot.cogs.get.return_value = None  # no VC

    consumed = await cog.receive_voice_answer_by_speaker("狗與露", "啊啊啊啊")

    assert consumed is False
    cog._channel.send.assert_called_once(), (
        "Silent-feedback bug: _channel.send must be called even without VoiceController."
    )


# ══════════════════════════════════════════════════════════════════════════════
# Bug: out_of_range → no feedback to user
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_out_of_range_sends_channel_message():
    """
    When submit_guess returns out_of_range (guess outside [low_bound, high_bound]),
    _channel.send() must be called with a user-visible error message.

    Before fix: only play_tts("超出範圍，再說一次") — which is blocked by game_mode
    guard — so the user sees nothing.
    """
    cog = _make_cog()
    session, engine, jack_id = await _bootstrap_guessing_human(cog)

    # Narrow the range so any guess outside [40, 60] is out_of_range
    session.low_bound = 40
    session.high_bound = 60

    mock_vc = MagicMock()
    mock_vc.play_tts = AsyncMock(return_value=None)
    cog.bot.cogs.get.return_value = mock_vc

    # Guess 10 is below low_bound=40 → out_of_range
    consumed = await cog.receive_voice_answer_by_speaker("狗與露", "10")

    assert consumed is False
    cog._channel.send.assert_called_once(), (
        "Silent-feedback bug: _channel.send must be called for out_of_range guess."
    )


# ══════════════════════════════════════════════════════════════════════════════
# Bug: boundary → no feedback to user
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_boundary_guess_sends_channel_message():
    """
    When submit_guess returns boundary (guess equals low_bound or high_bound),
    _channel.send() must be called with a user-visible error message.

    Before fix: only play_tts("不可以猜邊界") — blocked by game_mode guard.
    """
    cog = _make_cog()
    session, engine, jack_id = await _bootstrap_guessing_human(cog)

    # Set boundaries so that guessing exactly 1 (low_bound) hits boundary
    session.low_bound = 1
    session.high_bound = 99

    mock_vc = MagicMock()
    mock_vc.play_tts = AsyncMock(return_value=None)
    cog.bot.cogs.get.return_value = mock_vc

    # Guess the low boundary itself
    consumed = await cog.receive_voice_answer_by_speaker("狗與露", "1")

    assert consumed is False
    cog._channel.send.assert_called_once(), (
        "Silent-feedback bug: _channel.send must be called for boundary guess."
    )


# ══════════════════════════════════════════════════════════════════════════════
# Regression: valid wrong guess still sends embed (must not break)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_valid_wrong_guess_sends_embed():
    """
    A valid in-range non-boundary wrong guess (wrong_low/wrong_high) must still
    call _channel.send with an embed — this was already working and must not regress.
    """
    cog = _make_cog()
    session, engine, jack_id = await _bootstrap_guessing_human(cog)

    cog._play_sfx = AsyncMock()

    consumed = await cog.receive_voice_answer_by_speaker("狗與露", "30")

    assert consumed is True
    cog._channel.send.assert_called_once()
    call_kwargs = cog._channel.send.call_args
    assert call_kwargs.kwargs.get("embed") is not None or (
        call_kwargs.args and hasattr(call_kwargs.args[0], "title")
    ), "Valid wrong guess must send an embed, not plain text."
