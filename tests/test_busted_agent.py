"""Tests for BustedAgent — game-mode buzz-and-answer cog agent."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from intent_agents.busted_agent import BustedAgent
from intent_bus import IntentContext


def _ctx(raw="紅色", speaker="player1", mode="game"):
    return IntentContext(
        speaker=speaker, raw_text=raw, query=raw, original_raw=raw,
        wake_intent=None, stream_active=False,
        game_mode=(mode == "game"),
        is_owner=False, now=0.0, mode=mode,
    )


def _fake_bot(cog=None):
    bot = MagicMock()
    bot.cogs.get = MagicMock(side_effect=lambda name: cog if name == "BustedCog" else None)
    return bot


def _fake_cog(*, buzz_holder_id=None, suppress=False):
    cog = MagicMock()
    if buzz_holder_id is None:
        cog._session = None
    else:
        session = MagicMock()
        session.buzz_holder_id = buzz_holder_id
        cog._session = session
    cog.should_suppress_for_game = MagicMock(return_value=suppress)
    cog.receive_voice_answer_by_speaker = AsyncMock(return_value=True)
    return cog


def test_normal_mode_dense_zero():
    a = BustedAgent(_fake_bot(_fake_cog(buzz_holder_id="123")))
    bid = a.bid(_ctx(mode="normal"))
    assert bid.reason == "mode_mismatch:normal"


def test_cog_not_loaded():
    a = BustedAgent(_fake_bot(None))
    bid = a.bid(_ctx())
    assert bid.reason == "cog_not_loaded"


def test_no_buzz_window():
    a = BustedAgent(_fake_bot(_fake_cog(buzz_holder_id=None)))
    bid = a.bid(_ctx())
    assert bid.reason == "no_buzz_window"


def test_not_buzz_holder():
    a = BustedAgent(_fake_bot(_fake_cog(buzz_holder_id="123", suppress=True)))
    bid = a.bid(_ctx(speaker="other"))
    assert bid.reason == "not_buzz_holder"


def test_buzz_open_holder_bids_095():
    a = BustedAgent(_fake_bot(_fake_cog(buzz_holder_id="123", suppress=False)))
    bid = a.bid(_ctx(speaker="player1"))
    assert bid.confidence == 0.95
    assert bid.reason == "busted:buzz_open"


@pytest.mark.asyncio
async def test_handler_calls_cog_receive():
    cog = _fake_cog(buzz_holder_id="123", suppress=False)
    a = BustedAgent(_fake_bot(cog))
    bid = a.bid(_ctx(speaker="player1", raw="紅色"))
    await bid.handler()
    cog.receive_voice_answer_by_speaker.assert_awaited_once_with("player1", "紅色")


def test_empty_text_dense_zero():
    a = BustedAgent(_fake_bot(_fake_cog(buzz_holder_id="123")))
    bid = a.bid(_ctx(raw=""))
    assert bid.reason == "empty_text"
