"""Tests for Busted99Agent — game-mode intent agent vertical slice.

驗證：
  - 非 game mode → dense 0.0 with mode_mismatch
  - game mode + cog not loaded → cog_not_loaded
  - game mode + cog 存在但 _session is None → not_in_guessing_state
  - game mode + GUESSING + 非 current_guesser → not_current_guesser
  - game mode + GUESSING + current_guesser → bid 0.95 + handler 呼叫 receive_voice_answer
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from intent_agents.busted99_agent import Busted99Agent
from intent_bus import IntentContext


def _ctx(raw="馬文猜21", speaker="player1", mode="game"):
    return IntentContext(
        speaker=speaker, raw_text=raw, query=raw, original_raw=raw,
        wake_intent=None, stream_active=False,
        game_mode=(mode == "game"),
        is_owner=False, now=0.0, mode=mode,
    )


def _fake_bot(cog=None):
    bot = MagicMock()
    bot.cogs.get = MagicMock(side_effect=lambda name: cog if name == "Busted99Cog" else None)
    return bot


def _fake_cog(*, state_name="IDLE", suppress=False, receive_returns=True):
    """Build a fake Busted99Cog. state_name=None → no _session."""
    cog = MagicMock()
    if state_name is None:
        cog._session = None
    else:
        session = MagicMock()
        state = MagicMock()
        state.name = state_name
        session.state = state
        cog._session = session
    cog.should_suppress_for_game = MagicMock(return_value=suppress)
    cog.receive_voice_answer_by_speaker = AsyncMock(return_value=receive_returns)
    return cog


# ── Mode gating ───────────────────────────────────────────────────────────────

def test_normal_mode_dense_zero():
    agent = Busted99Agent(_fake_bot(cog=_fake_cog(state_name="GUESSING")))
    bid = agent.bid(_ctx(mode="normal"))
    assert bid.confidence == 0.0
    assert bid.reason == "mode_mismatch:normal"


def test_stream_mode_dense_zero():
    agent = Busted99Agent(_fake_bot(cog=_fake_cog(state_name="GUESSING")))
    bid = agent.bid(_ctx(mode="stream"))
    assert bid.reason == "mode_mismatch:stream"


# ── Cog state gating ──────────────────────────────────────────────────────────

def test_cog_not_loaded_dense_zero():
    agent = Busted99Agent(_fake_bot(cog=None))
    bid = agent.bid(_ctx())
    assert bid.confidence == 0.0
    assert bid.reason == "cog_not_loaded"


def test_no_session_dense_zero():
    agent = Busted99Agent(_fake_bot(cog=_fake_cog(state_name=None)))
    bid = agent.bid(_ctx())
    assert bid.reason == "not_in_guessing_state"


def test_session_not_in_guessing_dense_zero():
    agent = Busted99Agent(_fake_bot(cog=_fake_cog(state_name="WAITING_FOR_NEXT_ROUND")))
    bid = agent.bid(_ctx())
    assert bid.reason == "not_in_guessing_state"


def test_not_current_guesser_dense_zero():
    cog = _fake_cog(state_name="GUESSING", suppress=True)
    agent = Busted99Agent(_fake_bot(cog=cog))
    bid = agent.bid(_ctx(speaker="bystander"))
    assert bid.reason == "not_current_guesser"


# ── Happy path: bid 0.95 + handler ────────────────────────────────────────────

def test_guessing_with_current_guesser_bids_095():
    cog = _fake_cog(state_name="GUESSING", suppress=False)
    agent = Busted99Agent(_fake_bot(cog=cog))
    bid = agent.bid(_ctx(speaker="player1", raw="猜21"))
    assert bid.confidence == 0.95
    assert bid.reason == "busted99:guessing"
    assert bid.handler is not None


@pytest.mark.asyncio
async def test_handler_calls_cog_receive():
    cog = _fake_cog(state_name="GUESSING", suppress=False)
    agent = Busted99Agent(_fake_bot(cog=cog))
    bid = agent.bid(_ctx(speaker="player1", raw="猜21"))
    await bid.handler()
    cog.receive_voice_answer_by_speaker.assert_awaited_once_with("player1", "猜21")


def test_empty_text_dense_zero():
    cog = _fake_cog(state_name="GUESSING", suppress=False)
    agent = Busted99Agent(_fake_bot(cog=cog))
    bid = agent.bid(_ctx(raw=""))
    assert bid.reason == "empty_text"


# ── Bus dispatch integration sanity ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_bus_dispatches_busted99_winner_in_game_mode():
    """Verify the agent integrates with IntentBus dispatch correctly."""
    from intent_bus import IntentBus
    cog = _fake_cog(state_name="GUESSING", suppress=False)
    agent = Busted99Agent(_fake_bot(cog=cog))
    bus = IntentBus([agent])
    winner = await bus.dispatch(_ctx(speaker="player1", raw="猜21"))
    assert winner is not None
    assert winner.name == "busted99"
    assert winner.confidence == 0.95
    cog.receive_voice_answer_by_speaker.assert_awaited_once()


@pytest.mark.asyncio
async def test_bus_no_dispatch_in_normal_mode():
    """Busted99 在 normal mode 應該 bid 0.0 → bus no winner."""
    from intent_bus import IntentBus
    cog = _fake_cog(state_name="GUESSING", suppress=False)
    agent = Busted99Agent(_fake_bot(cog=cog))
    bus = IntentBus([agent])
    winner = await bus.dispatch(_ctx(mode="normal"))
    assert winner is None  # < MIN_CONFIDENCE
