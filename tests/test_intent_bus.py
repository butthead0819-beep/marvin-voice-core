"""TDD：IntentBus core — IntentContext / Bid / IntentBus dispatch 行為。"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from intent_bus import IntentBus, IntentContext, Bid


def _ctx(query="今天天氣怎樣", wake_intent=None, game_mode=False, is_owner=False, stream_active=False):
    return IntentContext(
        speaker="Alice",
        raw_text=query,
        query=query,
        original_raw=None,
        wake_intent=wake_intent,
        stream_active=stream_active,
        game_mode=game_mode,
        is_owner=is_owner,
        now=100.0,
    )


class _StubAgent:
    """Configurable test agent. bid_fn returns Bid|None when called."""
    def __init__(self, name, bid_fn=None):
        self.name = name
        self._bid_fn = bid_fn
        self.calls = 0
    def bid(self, ctx):
        self.calls += 1
        return self._bid_fn(ctx) if self._bid_fn else None


def _bid(name, confidence, handler=None):
    return Bid(name=name, confidence=confidence,
               handler=handler or AsyncMock(),
               reason=f"test:{name}")


# ── Empty / no-bid cases ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_returns_none_when_no_agents():
    bus = IntentBus([])
    result = await bus.dispatch(_ctx())
    assert result is None


@pytest.mark.asyncio
async def test_dispatch_returns_none_when_all_agents_pass():
    bus = IntentBus([_StubAgent("a", lambda c: None),
                     _StubAgent("b", lambda c: None)])
    result = await bus.dispatch(_ctx())
    assert result is None


# ── Single winner ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_calls_winner_handler():
    handler = AsyncMock()
    bus = IntentBus([_StubAgent("a", lambda c: _bid("a", 0.9, handler))])
    winner = await bus.dispatch(_ctx())
    assert winner is not None
    assert winner.name == "a"
    handler.assert_awaited_once()


# ── Max wins ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_picks_highest_confidence():
    h_low = AsyncMock()
    h_high = AsyncMock()
    bus = IntentBus([
        _StubAgent("low",  lambda c: _bid("low",  0.40, h_low)),
        _StubAgent("high", lambda c: _bid("high", 0.95, h_high)),
        _StubAgent("mid",  lambda c: _bid("mid",  0.70, AsyncMock())),
    ])
    winner = await bus.dispatch(_ctx())
    assert winner.name == "high"
    h_high.assert_awaited_once()
    h_low.assert_not_awaited()


# ── Min confidence threshold ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_returns_none_when_max_below_threshold():
    handler = AsyncMock()
    bus = IntentBus([_StubAgent("weak", lambda c: _bid("weak", 0.20, handler))])
    result = await bus.dispatch(_ctx())
    assert result is None
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_min_confidence_is_inclusive_at_threshold():
    """confidence == MIN_CONFIDENCE 應該 OK 算贏。"""
    bus = IntentBus.__new__(IntentBus)  # bypass __init__
    bus.agents = []
    bus.MIN_CONFIDENCE = 0.30
    bus.logger = logging.getLogger("test")

    handler = AsyncMock()
    bus.agents = [_StubAgent("edge", lambda c: _bid("edge", 0.30, handler))]
    winner = await bus.dispatch(_ctx())
    assert winner is not None
    handler.assert_awaited_once()


# ── Agent exception isolation ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_isolates_agent_exception():
    """一個 agent 的 bid() 炸了，其他 agents 還是要正常 bid。"""
    def _broken(ctx):
        raise RuntimeError("agent crashed")

    h_ok = AsyncMock()
    bus = IntentBus([
        _StubAgent("broken", _broken),
        _StubAgent("ok", lambda c: _bid("ok", 0.5, h_ok)),
    ])
    winner = await bus.dispatch(_ctx())
    assert winner is not None
    assert winner.name == "ok"
    h_ok.assert_awaited_once()


# ── Handler exception propagates ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_handler_exception_propagates():
    """handler() 炸了不該被吞 — caller 要看得到。"""
    async def _bad_handler():
        raise RuntimeError("handler bug")

    bus = IntentBus([_StubAgent("x", lambda c: _bid("x", 0.9, _bad_handler))])
    with pytest.raises(RuntimeError, match="handler bug"):
        await bus.dispatch(_ctx())


# ── Observability ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_logs_bid_summary(caplog):
    bus = IntentBus([
        _StubAgent("a", lambda c: _bid("a", 0.95)),
        _StubAgent("b", lambda c: _bid("b", 0.40)),
        _StubAgent("c", lambda c: None),
    ])
    with caplog.at_level(logging.INFO, logger="intent_bus"):
        await bus.dispatch(_ctx())
    # 應該至少有一條 log 同時含 winner + bids
    relevant = [r for r in caplog.records if "IntentBus" in r.message]
    assert relevant, "expected at least one [IntentBus] log line"
    combined = " ".join(r.message for r in relevant)
    assert "a=0.95" in combined or "a:0.95" in combined or "a 0.95" in combined
    assert "b=0.40" in combined or "b:0.40" in combined or "b 0.40" in combined
    assert "winner" in combined.lower() or "won" in combined.lower()


# ── Bid ordering: stable when tied ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_with_tie_picks_first_registered():
    """同分時取第一個註冊的（穩定排序），方便 debug。"""
    h1, h2 = AsyncMock(), AsyncMock()
    bus = IntentBus([
        _StubAgent("first",  lambda c: _bid("first",  0.80, h1)),
        _StubAgent("second", lambda c: _bid("second", 0.80, h2)),
    ])
    winner = await bus.dispatch(_ctx())
    assert winner.name == "first"
    h1.assert_awaited_once()
    h2.assert_not_awaited()


# ── IntentContext immutability ─────────────────────────────────────────────

def test_intent_context_is_frozen():
    """IntentContext 是 frozen dataclass — agent 不能誤改 state。"""
    ctx = _ctx()
    with pytest.raises((AttributeError, Exception)):  # FrozenInstanceError
        ctx.query = "tampered"  # type: ignore
