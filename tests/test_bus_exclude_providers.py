"""TDD — LLMContext.exclude_providers：使用者聽得到/看得到的生成硬排除 mistral
（ministral-8b 會吐舞台指示/markdown/半句斷掉，2026-10-10）。硬排除，不是排序——
被排除的 provider 完全不進 viable，即便它是唯一 bid 也不會被選中。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from llm_agents.base import (
    LLMAgent, LLMBid, LLMBus, LLMContext, NoLLMAvailable,
    SPOKEN_PURPOSES,
)


def _make_bid(confidence, provider, latency_ms=100, model="m", reason="h"):
    return LLMBid(
        confidence=confidence, provider=provider, model=model,
        estimated_latency_ms=latency_ms, estimated_cost_units=10, reason=reason,
    )


def _make_agent(name, confidence, response="ok", provider=None, latency_ms=100, priority=50, reason="h"):
    provider = provider or name
    agent = MagicMock(spec=LLMAgent)
    agent.name = name
    agent.priority = priority
    agent.purpose_compatible = frozenset()  # empty = 全 purpose
    agent.bid = MagicMock(return_value=_make_bid(confidence, provider, latency_ms, reason=reason))
    agent.handle = AsyncMock(return_value=response)
    return agent


@pytest.mark.asyncio
async def test_exclude_providers_removes_excluded_even_if_highest_confidence():
    mistral = _make_agent("mistral", 0.9, response="from_mistral")
    groq = _make_agent("groq", 0.5, response="from_groq")
    bus = LLMBus([mistral, groq])

    ctx = LLMContext(prompt="x", purpose="generate_greeting", exclude_providers=("mistral",))
    result = await bus.dispatch(ctx)

    assert result == "from_groq"
    groq.handle.assert_awaited_once()
    mistral.handle.assert_not_awaited()


@pytest.mark.asyncio
async def test_exclude_providers_all_excluded_raises_no_llm_available():
    mistral = _make_agent("mistral", 0.9, response="from_mistral")
    bus = LLMBus([mistral])

    ctx = LLMContext(prompt="x", purpose="generate_greeting", exclude_providers=("mistral",))
    with pytest.raises(NoLLMAvailable):
        await bus.dispatch(ctx)


@pytest.mark.asyncio
async def test_no_exclude_providers_keeps_mistral_behavior():
    mistral = _make_agent("mistral", 0.9, response="from_mistral")
    groq = _make_agent("groq", 0.5, response="from_groq")
    bus = LLMBus([mistral, groq])

    ctx = LLMContext(prompt="x", purpose="generate_greeting")
    result = await bus.dispatch(ctx)

    assert result == "from_mistral"
    mistral.handle.assert_awaited_once()


def test_spoken_purposes_membership():
    assert "generate_dynamic_system_msg" in SPOKEN_PURPOSES
    assert "summarize_window" not in SPOKEN_PURPOSES
