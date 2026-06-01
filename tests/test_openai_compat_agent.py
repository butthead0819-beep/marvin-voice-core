"""OpenAICompatAgent — 通用 OpenAI-compat provider agent（SambaNova/Together/OpenRouter）。

仿 test_cerebras_agent；驗 provider name 參數化、endpoint 命名、bid/handle、429 cooldown。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from llm_agents.base import LLMContext
from llm_agents.openai_compat_agent import OpenAICompatAgent
from llm_agents.quota_service import QuotaService
from llm_pool import CooldownAwarePool, PoolEndpoint


def _make_quota(provider="sambanova", *, cooldown_remaining=0.0, tpm_used=0):
    clock_holder = [1000.0]
    clock = lambda: clock_holder[0]
    ep_q = PoolEndpoint(name=f"{provider}-quick", client=MagicMock(),
                        model=f"{provider}-quick-model", tpm_budget=6000)
    ep_a = PoolEndpoint(name=f"{provider}-analyze", client=MagicMock(),
                        model=f"{provider}-analyze-model", tpm_budget=6000)
    pool = CooldownAwarePool([ep_q, ep_a], clock=clock)
    if tpm_used:
        pool.record_usage(ep_q, tpm_used)
        pool.record_usage(ep_a, tpm_used)
    if cooldown_remaining > 0:
        pool.mark_429(ep_q, retry_after=cooldown_remaining)
        pool.mark_429(ep_a, retry_after=cooldown_remaining)
    return QuotaService([pool]), ep_q, ep_a


def _chat_response(content="hi", total_tokens=10):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    resp.usage = MagicMock(total_tokens=total_tokens)
    return resp


# ── name / endpoint 參數化 ────────────────────────────────────────────────────

def test_agent_name_and_priority_from_init():
    quota, _, _ = _make_quota("together")
    agent = OpenAICompatAgent(quota, provider_name="together", priority=21)
    assert agent.name == "together"
    assert agent.priority == 21
    assert agent.providers == frozenset({"together"})


def test_bid_high_quality_routes_analyze_endpoint():
    quota, _, _ = _make_quota("openrouter")
    bid = OpenAICompatAgent(quota, provider_name="openrouter").bid(
        LLMContext(prompt="x", purpose="marvin_chat", min_quality="high"))
    assert bid.model == "openrouter-analyze-model"
    assert bid.provider == "openrouter"


def test_bid_balanced_routes_quick_endpoint():
    quota, _, _ = _make_quota("sambanova")
    bid = OpenAICompatAgent(quota, provider_name="sambanova").bid(
        LLMContext(prompt="x", purpose="cleaner"))
    assert bid.model == "sambanova-quick-model"


# ── dense 0.0 reasons ─────────────────────────────────────────────────────────

def test_bid_dense_zero_when_cooldown():
    quota, _, _ = _make_quota("sambanova", cooldown_remaining=30.0)
    bid = OpenAICompatAgent(quota, provider_name="sambanova").bid(
        LLMContext(prompt="x", purpose="cleaner"))
    assert bid.confidence == 0.0
    assert "cooldown" in bid.reason


def test_bid_dense_zero_when_endpoint_missing():
    # quota 只有 sambanova，問 together → endpoint_not_registered
    quota, _, _ = _make_quota("sambanova")
    bid = OpenAICompatAgent(quota, provider_name="together").bid(
        LLMContext(prompt="x", purpose="cleaner"))
    assert bid.confidence == 0.0
    assert "endpoint_not_registered" in bid.reason


def test_bid_confidence_lower_than_groq_cerebras():
    """備援定位：BASE_CONFIDENCE 0.50 < Groq/Cerebras 0.65。"""
    quota, _, _ = _make_quota("together")
    bid = OpenAICompatAgent(quota, provider_name="together").bid(
        LLMContext(prompt="x", purpose="marvin_chat"))
    assert 0.30 <= bid.confidence <= 0.50


# ── handle ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_calls_client_and_records_usage():
    quota, ep_q, _ = _make_quota("sambanova")
    ep_q.client.chat.completions.create = AsyncMock(return_value=_chat_response("answer", 33))
    agent = OpenAICompatAgent(quota, provider_name="sambanova")
    out = await agent.handle(LLMContext(prompt="q", purpose="cleaner"))
    assert out == "answer"
    ep_q.client.chat.completions.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_marks_429_on_rate_limit():
    quota, ep_q, _ = _make_quota("openrouter")
    ep_q.client.chat.completions.create = AsyncMock(
        side_effect=Exception("Error code: 429 rate limit"))
    agent = OpenAICompatAgent(quota, provider_name="openrouter")
    with pytest.raises(Exception):
        await agent.handle(LLMContext(prompt="q", purpose="cleaner"))
    # 429 後該 endpoint 進 cooldown → 下次 bid dense 0.0
    bid = agent.bid(LLMContext(prompt="q", purpose="cleaner"))
    assert bid.confidence == 0.0
    assert "cooldown" in bid.reason


@pytest.mark.asyncio
async def test_handle_json_mode_sets_response_format():
    quota, ep_q, _ = _make_quota("together")
    ep_q.client.chat.completions.create = AsyncMock(return_value=_chat_response('{"a":1}'))
    agent = OpenAICompatAgent(quota, provider_name="together")
    await agent.handle(LLMContext(prompt="q", purpose="cleaner", json_mode=True))
    kwargs = ep_q.client.chat.completions.create.call_args.kwargs
    assert kwargs["response_format"] == {"type": "json_object"}
