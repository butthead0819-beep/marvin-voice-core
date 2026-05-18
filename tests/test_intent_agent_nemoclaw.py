"""TDD：NemoClawAgent — 「龍蝦」喚醒詞直送 NemoClaw 子系統。

Confidence 規約：
  0.95 — owner + 龍蝦 regex 命中 (direct trigger)
  None — 非 owner、low confidence wake、無龍蝦詞
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_bus import IntentContext
from intent_agents.nemoclaw_agent import NemoClawAgent


def _ctx(query="龍蝦幫我查今天天氣", raw_text=None, wake_intent=None, is_owner=True, original_raw=None):
    raw = raw_text or query
    return IntentContext(
        speaker="Alice", raw_text=raw, query=query,
        original_raw=original_raw or raw,
        wake_intent=wake_intent, stream_active=False,
        game_mode=False, is_owner=is_owner, now=100.0,
    )


def _agent():
    ctrl = MagicMock()
    ctrl._handle_nemoclaw_query = AsyncMock()
    return NemoClawAgent(ctrl), ctrl


# ── owner + 龍蝦 → 高分出價 ───────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "龍蝦幫我查",
    "龍蝦你能不能",
    "龍蝦",
])
def test_owner_lobster_bids_high(text):
    agent, _ = _agent()
    bid = agent.bid(_ctx(query=text, original_raw=text))
    assert bid is not None
    assert bid.confidence == pytest.approx(0.95)
    assert "lobster" in bid.reason.lower() or "龍蝦" in bid.reason


def test_lobster_bid_checks_original_raw_first():
    """喚醒詞「龍蝦」可能在 original_raw 但不在 stripped query。"""
    agent, _ = _agent()
    bid = agent.bid(_ctx(query="幫我查今天天氣", original_raw="龍蝦，幫我查今天天氣"))
    assert bid is not None
    assert bid.confidence == pytest.approx(0.95)


# ── 非 owner → 不出價 ────────────────────────────────────────────────────

def test_non_owner_does_not_bid():
    agent, _ = _agent()
    bid = agent.bid(_ctx(is_owner=False))
    assert bid is None


# ── low confidence wake → 不出價 ──────────────────────────────────────────

@pytest.mark.parametrize("wake_intent", [0.30, 0.50, 0.79])
def test_low_confidence_wake_does_not_bid(wake_intent):
    agent, _ = _agent()
    bid = agent.bid(_ctx(wake_intent=wake_intent))
    assert bid is None


def test_threshold_wake_intent_does_bid():
    agent, _ = _agent()
    bid = agent.bid(_ctx(wake_intent=0.80))
    assert bid is not None


# ── 沒龍蝦 → 不出價（smart router 由 MarvinAgent handler 內處理） ────────

@pytest.mark.parametrize("text", [
    "幫我查今天天氣",
    "馬文你覺得呢",
    "今天天氣怎樣",
])
def test_no_lobster_no_bid(text):
    agent, _ = _agent()
    bid = agent.bid(_ctx(query=text, original_raw=text))
    assert bid is None


# ── handler ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handler_calls_handle_nemoclaw_query_with_query():
    agent, ctrl = _agent()
    ctx = _ctx(query="龍蝦幫我查天氣", original_raw="龍蝦，幫我查天氣")
    bid = agent.bid(ctx)
    assert bid is not None
    await bid.handler()
    ctrl._handle_nemoclaw_query.assert_awaited_once()
    args = ctrl._handle_nemoclaw_query.await_args
    assert args.args[0] == "Alice"
