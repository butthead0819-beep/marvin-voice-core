"""TDD — LLMContext.prefer_providers：DJ 串場優先 gemini_free → groq，

其他 provider（mistral 等）只在前兩家都不可用時兜底。只影響 viable 排序，
不放寬 _MIN_CONFIDENCE 門檻（bid()/dense 0.0 行為不變）。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from llm_agents.base import LLMAgent, LLMBid, LLMBus, LLMContext


def _make_bid(confidence, provider, latency_ms=100, model="m", reason="h"):
    return LLMBid(
        confidence=confidence, provider=provider, model=model,
        estimated_latency_ms=latency_ms, estimated_cost_units=10, reason=reason,
    )


def _make_agent(name, confidence, response="ok", provider=None, latency_ms=100, priority=50, raises=False, reason="h"):
    provider = provider or name
    agent = MagicMock(spec=LLMAgent)
    agent.name = name
    agent.priority = priority
    agent.purpose_compatible = frozenset()  # empty = 全 purpose
    agent.bid = MagicMock(return_value=_make_bid(confidence, provider, latency_ms, reason=reason))
    if raises:
        agent.handle = AsyncMock(side_effect=RuntimeError("boom"))
    else:
        agent.handle = AsyncMock(return_value=response)
    return agent


@pytest.mark.asyncio
async def test_prefer_providers_picks_lower_confidence_preferred_provider():
    mistral = _make_agent("mistral", 0.9, response="from_mistral")
    groq = _make_agent("groq", 0.8, response="from_groq")
    gemini_free = _make_agent("gemini_free", 0.7, response="from_gemini_free")
    bus = LLMBus([mistral, groq, gemini_free])

    ctx = LLMContext(prompt="x", purpose="dj_quip", prefer_providers=("gemini_free", "groq"))
    result = await bus.dispatch(ctx)

    assert result == "from_gemini_free"
    gemini_free.handle.assert_awaited_once()
    mistral.handle.assert_not_awaited()
    groq.handle.assert_not_awaited()


@pytest.mark.asyncio
async def test_prefer_providers_failover_to_next_preferred_not_fallback():
    mistral = _make_agent("mistral", 0.9, response="from_mistral")
    groq = _make_agent("groq", 0.8, response="from_groq")
    gemini_free = _make_agent("gemini_free", 0.7, raises=True)
    bus = LLMBus([mistral, groq, gemini_free])

    ctx = LLMContext(prompt="x", purpose="dj_quip", prefer_providers=("gemini_free", "groq"))
    result = await bus.dispatch(ctx)

    assert result == "from_groq"
    mistral.handle.assert_not_awaited()


@pytest.mark.asyncio
async def test_prefer_providers_both_not_viable_falls_back_to_mistral():
    mistral = _make_agent("mistral", 0.9, response="from_mistral")
    groq = _make_agent("groq", 0.0, reason="cooldown")
    gemini_free = _make_agent("gemini_free", 0.0, reason="cooldown")
    bus = LLMBus([mistral, groq, gemini_free])

    ctx = LLMContext(prompt="x", purpose="dj_quip", prefer_providers=("gemini_free", "groq"))
    result = await bus.dispatch(ctx)

    assert result == "from_mistral"


@pytest.mark.asyncio
async def test_empty_prefer_providers_keeps_confidence_order():
    mistral = _make_agent("mistral", 0.9, response="from_mistral")
    groq = _make_agent("groq", 0.8, response="from_groq")
    gemini_free = _make_agent("gemini_free", 0.7, response="from_gemini_free")
    bus = LLMBus([mistral, groq, gemini_free])

    ctx = LLMContext(prompt="x", purpose="dj_quip")
    result = await bus.dispatch(ctx)

    assert result == "from_mistral"
