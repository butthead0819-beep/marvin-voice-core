"""Tests for TurtleSoupAgent — 海龜湯 game agent."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from intent_agents.turtle_soup_agent import TurtleSoupAgent
from intent_bus import IntentContext


def _ctx(raw="請問是男的嗎", speaker="player1", mode="game"):
    return IntentContext(
        speaker=speaker, raw_text=raw, query=raw, original_raw=raw,
        wake_intent=None, stream_active=False,
        game_mode=(mode == "game"),
        is_owner=False, now=0.0, mode=mode,
    )


def _fake_bot(cog=None):
    bot = MagicMock()
    bot.cogs.get = MagicMock(side_effect=lambda name: cog if name == "TurtleSoupCog" else None)
    return bot


def _fake_cog(*, active=False, state_name="ASKING"):
    cog = MagicMock()
    cog.is_active = MagicMock(return_value=active)
    if state_name is None:
        cog._session = None
    else:
        session = MagicMock()
        state = MagicMock()
        state.name = state_name
        session.state = state
        cog._session = session
    cog.receive_voice_answer_by_speaker = AsyncMock(return_value=True)
    return cog


def test_normal_mode_dense_zero():
    a = TurtleSoupAgent(_fake_bot(_fake_cog(active=True)))
    bid = a.bid(_ctx(mode="normal"))
    assert bid.reason == "mode_mismatch:normal"


def test_cog_not_loaded():
    a = TurtleSoupAgent(_fake_bot(None))
    bid = a.bid(_ctx())
    assert bid.reason == "cog_not_loaded"


def test_inactive_cog():
    a = TurtleSoupAgent(_fake_bot(_fake_cog(active=False, state_name="IDLE")))
    bid = a.bid(_ctx())
    assert bid.reason == "not_active"


def test_active_but_not_asking_state():
    """Active in JOINING or PRESENTING → bid 0.0, only ASKING consumes voice."""
    a = TurtleSoupAgent(_fake_bot(_fake_cog(active=True, state_name="PRESENTING")))
    bid = a.bid(_ctx())
    assert bid.reason == "not_in_asking_state"


def test_active_and_asking_bids_095():
    a = TurtleSoupAgent(_fake_bot(_fake_cog(active=True, state_name="ASKING")))
    bid = a.bid(_ctx(speaker="player1", raw="請問是男的嗎"))
    assert bid.confidence == 0.95
    assert bid.reason == "turtle_soup:asking"


@pytest.mark.asyncio
async def test_handler_calls_cog_receive():
    cog = _fake_cog(active=True, state_name="ASKING")
    a = TurtleSoupAgent(_fake_bot(cog))
    bid = a.bid(_ctx(speaker="player1", raw="是男的嗎"))
    await bid.handler()
    cog.receive_voice_answer_by_speaker.assert_awaited_once_with("player1", "是男的嗎")


def test_empty_text_dense_zero():
    a = TurtleSoupAgent(_fake_bot(_fake_cog(active=True)))
    bid = a.bid(_ctx(raw=""))
    assert bid.reason == "empty_text"
