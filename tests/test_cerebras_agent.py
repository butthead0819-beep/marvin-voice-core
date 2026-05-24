"""TDD — CerebrasAgent (Plan C7).

Cerebras 是 OpenAI-compatible，結構跟 GroqAgent 幾乎一樣。差異:
- endpoints: cerebras-quick (llama3.1-8b) / cerebras-analyze (qwen-3-235b)
- tpm_budget 大: 60000 (vs Groq 6000)
- 延遲更短（Cerebras 號稱「超高速」）

5 類測試 + F5 typo regression (照 GroqAgent 範本).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from llm_agents.quota_service import QuotaService
from llm_pool import CooldownAwarePool, PoolEndpoint


def _make_quota(*, available=True, tpm_used=0, cooldown_remaining=0.0):
    """Build a QuotaService with cerebras-quick + cerebras-analyze endpoints."""
    clock_holder = [1000.0]
    clock = lambda: clock_holder[0]
    quick_client = MagicMock(name="quick_client")
    analyze_client = MagicMock(name="analyze_client")
    ep_q = PoolEndpoint(name="cerebras-quick", client=quick_client,
                        model="llama3.1-8b", tpm_budget=60000)
    ep_a = PoolEndpoint(name="cerebras-analyze", client=analyze_client,
                        model="qwen-3-235b-a22b-instruct-2507", tpm_budget=60000)
    pool = CooldownAwarePool([ep_q, ep_a], clock=clock)
    if tpm_used:
        pool.record_usage(ep_q, tpm_used)
        pool.record_usage(ep_a, tpm_used)
    if cooldown_remaining > 0:
        pool.mark_429(ep_q, retry_after=cooldown_remaining)
        pool.mark_429(ep_a, retry_after=cooldown_remaining)
    return QuotaService([pool]), ep_q, ep_a


def _make_chat_response(content="hello", total_tokens=42):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    resp.usage = MagicMock(total_tokens=total_tokens)
    return resp


# ---------------------------------------------------------------------------
# 1. Mode / min_quality 分流
# ---------------------------------------------------------------------------

def test_bid_routes_high_quality_to_qwen_endpoint():
    from llm_agents.base import LLMContext
    from llm_agents.cerebras_agent import CerebrasAgent
    quota, _, _ = _make_quota()
    bid = CerebrasAgent(quota).bid(LLMContext(prompt="x", purpose="marvin_chat", min_quality="high"))
    assert "qwen" in bid.model.lower() or "235b" in bid.model.lower()


def test_bid_routes_fast_to_8b_endpoint():
    from llm_agents.base import LLMContext
    from llm_agents.cerebras_agent import CerebrasAgent
    quota, _, _ = _make_quota()
    bid = CerebrasAgent(quota).bid(LLMContext(prompt="x", purpose="cleaner", min_quality="fast"))
    assert bid.model == "llama3.1-8b"


def test_bid_routes_balanced_to_8b_default():
    from llm_agents.base import LLMContext
    from llm_agents.cerebras_agent import CerebrasAgent
    quota, _, _ = _make_quota()
    bid = CerebrasAgent(quota).bid(LLMContext(prompt="x", purpose="marvin_chat"))
    assert bid.model == "llama3.1-8b"


# ---------------------------------------------------------------------------
# 2. Resource availability — dense 0.0 when not available
# ---------------------------------------------------------------------------

def test_bid_dense_zero_when_cooldown():
    from llm_agents.base import LLMContext
    from llm_agents.cerebras_agent import CerebrasAgent
    quota, _, _ = _make_quota(cooldown_remaining=60.0)
    bid = CerebrasAgent(quota).bid(LLMContext(prompt="x", purpose="cleaner"))
    assert bid.confidence == 0.0
    assert "cooldown" in bid.reason.lower()


def test_bid_dense_zero_when_tpm_above_headroom():
    """Cerebras tpm_budget=60000, headroom 0.75 = 45000 → 50000 used 該 dense 0.0."""
    from llm_agents.base import LLMContext
    from llm_agents.cerebras_agent import CerebrasAgent
    quota, _, _ = _make_quota(tpm_used=50000)
    bid = CerebrasAgent(quota).bid(LLMContext(prompt="x", purpose="cleaner"))
    assert bid.confidence == 0.0
    assert "tpm" in bid.reason.lower()


# ---------------------------------------------------------------------------
# 3. Distinct dense 0.0 reasons
# ---------------------------------------------------------------------------

def test_bid_distinct_dense_zero_reasons():
    from llm_agents.base import LLMContext
    from llm_agents.cerebras_agent import CerebrasAgent

    reasons = set()
    reasons.add(CerebrasAgent(QuotaService([])).bid(LLMContext(prompt="x", purpose="cleaner")).reason)
    q_cool, _, _ = _make_quota(cooldown_remaining=60.0)
    reasons.add(CerebrasAgent(q_cool).bid(LLMContext(prompt="x", purpose="cleaner")).reason)
    q_hot, _, _ = _make_quota(tpm_used=50000)
    reasons.add(CerebrasAgent(q_hot).bid(LLMContext(prompt="x", purpose="cleaner")).reason)
    assert len(reasons) == 3, f"預期 3 distinct reasons, 實際: {reasons}"


# ---------------------------------------------------------------------------
# 4. Happy path
# ---------------------------------------------------------------------------

def test_bid_happy_path_confidence_above_threshold():
    from llm_agents.base import LLMContext
    from llm_agents.cerebras_agent import CerebrasAgent
    quota, _, _ = _make_quota()
    bid = CerebrasAgent(quota).bid(LLMContext(prompt="x", purpose="cleaner"))
    assert bid.confidence >= 0.30
    assert bid.provider == "cerebras"


def test_bid_lower_latency_than_groq_estimate():
    """Cerebras 號稱比 Groq 快，latency 估計該更低."""
    from llm_agents.base import LLMContext
    from llm_agents.cerebras_agent import CerebrasAgent
    from llm_agents.groq_agent import GroqAgent
    quota_c, _, _ = _make_quota()
    # 替 Groq 也做一個 quota
    from llm_pool import CooldownAwarePool, PoolEndpoint
    g_ep_q = PoolEndpoint(name="groq-quick", client=MagicMock(), model="llama-3.1-8b-instant", tpm_budget=6000)
    g_ep_a = PoolEndpoint(name="groq-analyze", client=MagicMock(), model="llama-3.3-70b-versatile", tpm_budget=6000)
    quota_g = QuotaService([CooldownAwarePool([g_ep_q, g_ep_a], clock=lambda: 1000.0)])

    ctx = LLMContext(prompt="x", purpose="cleaner")
    cerebras_bid = CerebrasAgent(quota_c).bid(ctx)
    groq_bid = GroqAgent(quota_g).bid(ctx)
    assert cerebras_bid.estimated_latency_ms < groq_bid.estimated_latency_ms, \
        f"Cerebras 該比 Groq 快, 實際: cerebras={cerebras_bid.estimated_latency_ms}ms groq={groq_bid.estimated_latency_ms}ms"


# ---------------------------------------------------------------------------
# 5. Handler integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_calls_cerebras_client_and_records_usage():
    from llm_agents.base import LLMContext
    from llm_agents.cerebras_agent import CerebrasAgent
    quota, ep_q, _ = _make_quota()
    ep_q.client.chat.completions.create = AsyncMock(
        return_value=_make_chat_response("cerebras says", total_tokens=88))
    result = await CerebrasAgent(quota).handle(LLMContext(prompt="hi", purpose="cleaner"))
    assert result == "cerebras says"
    ep_q.client.chat.completions.create.assert_awaited_once()
    assert quota.state("cerebras-quick").tpm_used == 88


@pytest.mark.asyncio
async def test_handle_marks_429_on_rate_limit():
    from llm_agents.base import LLMContext
    from llm_agents.cerebras_agent import CerebrasAgent
    quota, ep_q, _ = _make_quota()
    ep_q.client.chat.completions.create = AsyncMock(
        side_effect=RuntimeError("429 rate limit, retry in 20s"))
    with pytest.raises(Exception):
        await CerebrasAgent(quota).handle(LLMContext(prompt="x", purpose="cleaner"))
    assert quota.state("cerebras-quick").cooldown_remaining_s > 0


# ---------------------------------------------------------------------------
# F5 typo regression
# ---------------------------------------------------------------------------

def test_bid_works_for_typo_purpose():
    from llm_agents.base import LLMContext
    from llm_agents.cerebras_agent import CerebrasAgent
    quota, _, _ = _make_quota()
    bid = CerebrasAgent(quota).bid(LLMContext(prompt="x", purpose="marvine_chat"))
    assert bid.confidence > 0.0
    assert bid.provider == "cerebras"


# ---------------------------------------------------------------------------
# Bus integration — Groq + Cerebras 同台 bid, max wins
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_groq_and_cerebras_both_in_bus_max_wins():
    """Groq + Cerebras 同 bus 內，cerebras 有更大 TPM budget → 同負載下 cerebras 該贏."""
    from llm_agents.base import LLMBus, LLMContext
    from llm_agents.cerebras_agent import CerebrasAgent
    from llm_agents.groq_agent import GroqAgent

    # Cerebras quota: 0 used / 60000 budget → tpm_ratio ~0
    quota_c, ep_cq, _ = _make_quota()
    ep_cq.client.chat.completions.create = AsyncMock(return_value=_make_chat_response("cerebras_won"))

    # Groq quota: 4000 used / 6000 budget → tpm_ratio ~0.67 (壓力大但未 dense 0.0)
    g_ep_q = PoolEndpoint(name="groq-quick", client=AsyncMock(), model="llama-3.1-8b-instant", tpm_budget=6000)
    g_ep_a = PoolEndpoint(name="groq-analyze", client=AsyncMock(), model="llama-3.3-70b-versatile", tpm_budget=6000)
    g_pool = CooldownAwarePool([g_ep_q, g_ep_a], clock=lambda: 1000.0)
    g_pool.record_usage(g_ep_q, 4000)
    quota_g = QuotaService([g_pool])

    bus = LLMBus([GroqAgent(quota_g), CerebrasAgent(quota_c)])
    result = await bus.dispatch(LLMContext(prompt="x", purpose="cleaner"))
    assert result == "cerebras_won", "Cerebras headroom 比 Groq 大時該贏"
