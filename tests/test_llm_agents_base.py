"""TDD — llm_agents.base 核心 contract.

範圍 (Plan C1):
- LLMContext / LLMBid dataclasses
- LLMAgent base：sync bid() ≤5ms 契約
- LLMBus dispatch：max confidence 勝、無 available 拋 NoLLMAvailable、tiebreak by latency
- F3: short-circuit 只 bid 前 K=3 agent (priority sorted)
- F5: KNOWN_PURPOSES warning（typo purpose 不擋 dispatch 但留 log warning）
- F4: stickiness 同 speaker 5 min 偏好同 provider，confidence +0.10 bonus
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bid(confidence, provider, latency_ms=100, model="m", reason="h"):
    """Build LLMBid fresh — frozen dataclass，不能 mutate。"""
    from llm_agents.base import LLMBid
    return LLMBid(
        confidence=confidence, provider=provider, model=model,
        estimated_latency_ms=latency_ms, estimated_cost_units=10, reason=reason,
    )


def _make_agent(name, confidence, response="ok", provider=None, latency_ms=100, priority=50):
    from llm_agents.base import LLMAgent
    provider = provider or name
    agent = MagicMock(spec=LLMAgent)
    agent.name = name
    agent.priority = priority
    agent.purpose_compatible = frozenset()  # empty = 全 purpose
    agent.bid = MagicMock(return_value=_make_bid(confidence, provider, latency_ms))
    agent.handle = AsyncMock(return_value=response)
    return agent


# ---------------------------------------------------------------------------
# Dataclass shape
# ---------------------------------------------------------------------------

def test_llm_context_minimum_fields():
    from llm_agents.base import LLMContext
    ctx = LLMContext(prompt="hi", purpose="marvin_chat")
    assert ctx.prompt == "hi"
    assert ctx.purpose == "marvin_chat"
    # defaults
    assert ctx.speaker is None
    assert ctx.min_quality == "balanced"
    assert ctx.latency_budget_ms is None


def test_llm_bid_minimum_fields():
    bid = _make_bid(0.7, "groq")
    assert bid.confidence == 0.7
    assert bid.provider == "groq"
    assert bid.reason == "h"


# ---------------------------------------------------------------------------
# Bid contract: sync (NOT coroutine)
# ---------------------------------------------------------------------------

def test_bid_must_be_sync():
    """5ms sync 契約：bid 必須是 sync method，handle 才是 async。"""
    from llm_agents.base import LLMAgent
    assert not inspect.iscoroutinefunction(LLMAgent.bid), \
        "LLMAgent.bid 不能是 coroutine — 5ms sync 契約"
    assert inspect.iscoroutinefunction(LLMAgent.handle), \
        "LLMAgent.handle 必須是 coroutine"


# ---------------------------------------------------------------------------
# Bus core dispatch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bus_dispatches_to_highest_confidence():
    from llm_agents.base import LLMBus, LLMContext
    a = _make_agent("a", 0.5, response="from_a")
    b = _make_agent("b", 0.8, response="from_b")
    c = _make_agent("c", 0.3, response="from_c")
    bus = LLMBus([a, b, c])
    result = await bus.dispatch(LLMContext(prompt="x", purpose="marvin_chat"))
    assert result == "from_b"
    b.handle.assert_awaited_once()
    a.handle.assert_not_awaited()
    c.handle.assert_not_awaited()


@pytest.mark.asyncio
async def test_bus_raises_when_all_below_threshold():
    """全 dense 0.0 / 低於 MIN_CONFIDENCE → NoLLMAvailable，caller 兜底。"""
    from llm_agents.base import LLMBus, LLMContext, NoLLMAvailable
    a = _make_agent("a", 0.0)
    b = _make_agent("b", 0.1)
    bus = LLMBus([a, b])
    with pytest.raises(NoLLMAvailable):
        await bus.dispatch(LLMContext(prompt="x", purpose="marvin_chat"))


@pytest.mark.asyncio
async def test_bus_tiebreak_by_latency():
    """同 confidence → estimated_latency_ms 小的贏。"""
    from llm_agents.base import LLMBus, LLMContext
    slow = _make_agent("slow", 0.5, response="slow_r", latency_ms=2000)
    fast = _make_agent("fast", 0.5, response="fast_r", latency_ms=100)
    bus = LLMBus([slow, fast])
    result = await bus.dispatch(LLMContext(prompt="x", purpose="marvin_chat"))
    assert result == "fast_r"


# ---------------------------------------------------------------------------
# F3: short-circuit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bus_short_circuit_only_bids_first_k_agents():
    """F3 — bus 預設 _SHORT_CIRCUIT_AFTER=3 只 bid 前 3 個 agent (按 priority 排序)。"""
    from llm_agents.base import LLMBus, LLMContext
    agents = []
    for i in range(7):
        # priority i=0..6（小的 bid 先）
        a = _make_agent(f"a{i}", confidence=0.5, priority=i, response=f"r{i}")
        agents.append(a)
    bus = LLMBus(agents)
    await bus.dispatch(LLMContext(prompt="x", purpose="marvin_chat"))
    for i in range(3):
        agents[i].bid.assert_called_once(), f"agent {i} 該被 bid (前 K)"
    for i in range(3, 7):
        agents[i].bid.assert_not_called(), f"agent {i} 不該被 bid (超過 K)"


# ---------------------------------------------------------------------------
# F5: KNOWN_PURPOSES warning
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_purpose_logs_warning_but_still_dispatches(caplog):
    """F5 — typo purpose 不擋 dispatch（caller 兜底會出問題），但留 warning 在 log。"""
    from llm_agents.base import LLMBus, LLMContext
    a = _make_agent("a", 0.7, response="ok")
    bus = LLMBus([a])
    with caplog.at_level(logging.WARNING, logger="MarvinBot.LLMBus"):
        result = await bus.dispatch(LLMContext(prompt="x", purpose="marvine_chat"))  # typo
    assert result == "ok"
    assert any(
        "unknown purpose" in rec.message.lower() and "marvine_chat" in rec.message
        for rec in caplog.records
    ), f"預期 warning 含 'unknown purpose' 跟 typo 字串，實際：{[r.message for r in caplog.records]}"


@pytest.mark.asyncio
async def test_known_purpose_no_warning(caplog):
    from llm_agents.base import LLMBus, LLMContext
    a = _make_agent("a", 0.7, response="ok")
    bus = LLMBus([a])
    with caplog.at_level(logging.WARNING, logger="MarvinBot.LLMBus"):
        await bus.dispatch(LLMContext(prompt="x", purpose="marvin_chat"))
    assert not any(
        "unknown purpose" in rec.message.lower() for rec in caplog.records
    )


def test_known_purposes_set_contains_expected_baseline():
    """確保白名單至少含 Phase 1 預期的核心 purpose（grep `_dispatch_via_bus` callers 後 Phase 2 收斂時刪減）。"""
    from llm_agents.base import KNOWN_PURPOSES
    expected_baseline = {"marvin_chat", "cleaner", "wake_classify"}
    assert expected_baseline <= KNOWN_PURPOSES, \
        f"baseline purposes 該全在 KNOWN_PURPOSES 內，缺：{expected_baseline - KNOWN_PURPOSES}"


# ---------------------------------------------------------------------------
# F4: stickiness
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stickiness_keeps_same_provider_for_same_speaker():
    """F4 — 第一次 groq 贏；第二次 gemini bid 0.75 > groq 0.70 但 stickiness +0.10 翻盤 → groq 繼續贏。"""
    from llm_agents.base import LLMBus, LLMContext
    groq = _make_agent("groq", 0.7, provider="groq", response="groq_r")
    gemini = _make_agent("gemini", 0.5, provider="gemini", response="gemini_r")
    bus = LLMBus([groq, gemini])

    # 第一次：groq 0.7 > gemini 0.5
    r1 = await bus.dispatch(LLMContext(prompt="x", purpose="marvin_chat", speaker="alice"))
    assert r1 == "groq_r"

    # 翻 bid：gemini 變強
    groq.bid.return_value = _make_bid(0.70, "groq")
    gemini.bid.return_value = _make_bid(0.75, "gemini")

    # 沒 stickiness 的話 gemini (0.75) 會贏
    # 有 stickiness groq 上次贏家 → 0.70 + 0.10 = 0.80 → groq 繼續贏
    r2 = await bus.dispatch(LLMContext(prompt="x", purpose="marvin_chat", speaker="alice"))
    assert r2 == "groq_r", "stickiness 沒生效"


@pytest.mark.asyncio
async def test_stickiness_per_speaker():
    """alice 的 stickiness 不影響 bob。"""
    from llm_agents.base import LLMBus, LLMContext
    groq = _make_agent("groq", 0.7, provider="groq", response="groq_r")
    gemini = _make_agent("gemini", 0.5, provider="gemini", response="gemini_r")
    bus = LLMBus([groq, gemini])

    # alice 用 groq
    await bus.dispatch(LLMContext(prompt="x", purpose="marvin_chat", speaker="alice"))

    # 翻 bid 讓 gemini 強
    groq.bid.return_value = _make_bid(0.70, "groq")
    gemini.bid.return_value = _make_bid(0.75, "gemini")

    # bob 沒前史 → stickiness 不適用 → gemini 0.75 贏
    r_bob = await bus.dispatch(LLMContext(prompt="x", purpose="marvin_chat", speaker="bob"))
    assert r_bob == "gemini_r", "bob 沒前史不該繼承 alice 的 stickiness"


@pytest.mark.asyncio
async def test_stickiness_expires_after_ttl(monkeypatch):
    """F4 — _PROVIDER_STICKINESS_TTL=300 秒；超過 → bonus 失效。"""
    from llm_agents import base as base_mod
    from llm_agents.base import LLMBus, LLMContext

    # 用 monkeypatch 把 time.monotonic / time.time 一次性快轉
    now = [1000.0]
    monkeypatch.setattr(base_mod.time, "monotonic", lambda: now[0])

    groq = _make_agent("groq", 0.7, provider="groq", response="groq_r")
    gemini = _make_agent("gemini", 0.5, provider="gemini", response="gemini_r")
    bus = LLMBus([groq, gemini])
    await bus.dispatch(LLMContext(prompt="x", purpose="marvin_chat", speaker="alice"))

    # 翻 bid
    groq.bid.return_value = _make_bid(0.70, "groq")
    gemini.bid.return_value = _make_bid(0.75, "gemini")

    # 跳過 TTL (300s) + 1
    now[0] += 301
    r2 = await bus.dispatch(LLMContext(prompt="x", purpose="marvin_chat", speaker="alice"))
    assert r2 == "gemini_r", "TTL 過了 stickiness 該失效，gemini 該贏"


@pytest.mark.asyncio
async def test_stickiness_no_speaker_no_effect():
    """speaker=None → stickiness 完全不適用（系統呼叫如 background task 沒人講話）。"""
    from llm_agents.base import LLMBus, LLMContext
    groq = _make_agent("groq", 0.7, provider="groq", response="groq_r")
    gemini = _make_agent("gemini", 0.5, provider="gemini", response="gemini_r")
    bus = LLMBus([groq, gemini])

    # 第一次（沒 speaker）
    await bus.dispatch(LLMContext(prompt="x", purpose="marvin_chat", speaker=None))

    groq.bid.return_value = _make_bid(0.70, "groq")
    gemini.bid.return_value = _make_bid(0.75, "gemini")

    # 第二次（沒 speaker）— 沒 stickiness 應用 → gemini 贏
    r2 = await bus.dispatch(LLMContext(prompt="x", purpose="marvin_chat", speaker=None))
    assert r2 == "gemini_r", "speaker=None 不該觸發 stickiness"
