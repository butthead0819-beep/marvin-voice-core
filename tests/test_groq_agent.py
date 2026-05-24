"""TDD — GroqAgent: 第一個 LLMAgent concrete impl (Plan C3).

5 類測試（CLAUDE.md IntentBus 規範）：
1. mode_compatible / purpose gate — GroqAgent 全 purpose 支援，但 min_quality 分流到不同 endpoint
2. resource availability — quota state 不 available → dense 0.0
3. state failure — distinct dense 0.0 reasons (cooldown / tpm_high / endpoint_not_registered)
4. happy path — bid 數值合理
5. handler integration — async handle 呼叫 Groq client，成功時 record_usage、失敗時 mark_429

F5 typo regression：unknown purpose 不擋 bid（只 LLMBus 層 warning）。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from llm_agents.quota_service import QuotaService
from llm_pool import CooldownAwarePool, PoolEndpoint


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_quota(*, available=True, tpm_used=0, cooldown_remaining=0.0):
    """Build a QuotaService with one groq-quick + one groq-analyze endpoint."""
    clock_holder = [1000.0]
    clock = lambda: clock_holder[0]

    quick_client = MagicMock(name="quick_client")
    analyze_client = MagicMock(name="analyze_client")
    ep_q = PoolEndpoint(name="groq-quick", client=quick_client,
                        model="llama-3.1-8b-instant", tpm_budget=6000)
    ep_a = PoolEndpoint(name="groq-analyze", client=analyze_client,
                        model="llama-3.3-70b-versatile", tpm_budget=6000)
    pool = CooldownAwarePool([ep_q, ep_a], clock=clock)
    if tpm_used:
        pool.record_usage(ep_q, tpm_used)
        pool.record_usage(ep_a, tpm_used)
    if cooldown_remaining > 0:
        pool.mark_429(ep_q, retry_after=cooldown_remaining)
        pool.mark_429(ep_a, retry_after=cooldown_remaining)
    return QuotaService([pool]), ep_q, ep_a, clock_holder


def _make_chat_response(content="hello", total_tokens=42):
    """Mock OpenAI-compatible chat completion response."""
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    resp.usage = MagicMock(total_tokens=total_tokens)
    return resp


# ---------------------------------------------------------------------------
# 1. Mode / purpose — min_quality 分流到 endpoint
# ---------------------------------------------------------------------------

def test_bid_routes_high_quality_to_70b_endpoint():
    from llm_agents.base import LLMContext
    from llm_agents.groq_agent import GroqAgent
    quota, _, _, _ = _make_quota()
    agent = GroqAgent(quota)
    bid = agent.bid(LLMContext(prompt="x", purpose="marvin_chat", min_quality="high"))
    assert bid.model == "llama-3.3-70b-versatile"


def test_bid_routes_fast_quality_to_8b_endpoint():
    from llm_agents.base import LLMContext
    from llm_agents.groq_agent import GroqAgent
    quota, _, _, _ = _make_quota()
    agent = GroqAgent(quota)
    bid = agent.bid(LLMContext(prompt="x", purpose="cleaner", min_quality="fast"))
    assert bid.model == "llama-3.1-8b-instant"


def test_bid_routes_balanced_to_8b_endpoint_default():
    """min_quality='balanced' (default) → 預設 8b — 不會 fall through 到最強 model 燒預算."""
    from llm_agents.base import LLMContext
    from llm_agents.groq_agent import GroqAgent
    quota, _, _, _ = _make_quota()
    agent = GroqAgent(quota)
    bid = agent.bid(LLMContext(prompt="x", purpose="marvin_chat"))
    assert bid.model == "llama-3.1-8b-instant"


# ---------------------------------------------------------------------------
# 2. Resource availability — quota state 不 available → dense 0.0
# ---------------------------------------------------------------------------

def test_bid_dense_zero_when_cooldown():
    from llm_agents.base import LLMContext
    from llm_agents.groq_agent import GroqAgent
    quota, _, _, _ = _make_quota(cooldown_remaining=60.0)
    agent = GroqAgent(quota)
    bid = agent.bid(LLMContext(prompt="x", purpose="cleaner"))
    assert bid.confidence == 0.0
    assert "cooldown" in bid.reason.lower()


def test_bid_dense_zero_when_tpm_above_headroom():
    """TPM_HEADROOM=0.75，6000*0.75=4500，tpm_used=5000 → 不 available."""
    from llm_agents.base import LLMContext
    from llm_agents.groq_agent import GroqAgent
    quota, _, _, _ = _make_quota(tpm_used=5000)
    agent = GroqAgent(quota)
    bid = agent.bid(LLMContext(prompt="x", purpose="cleaner"))
    assert bid.confidence == 0.0
    assert "tpm" in bid.reason.lower()


# ---------------------------------------------------------------------------
# 3. State failure — distinct dense 0.0 reasons
# ---------------------------------------------------------------------------

def test_bid_dense_zero_distinct_reasons():
    """每個拒絕原因該有 distinct reason 字串（CLAUDE.md 規範 — 禁全寫 'no_match'）."""
    from llm_agents.base import LLMContext
    from llm_agents.groq_agent import GroqAgent

    reasons = set()

    # endpoint_not_registered
    empty_quota = QuotaService([])
    agent_empty = GroqAgent(empty_quota)
    reasons.add(agent_empty.bid(LLMContext(prompt="x", purpose="cleaner")).reason)

    # cooldown
    q_cool, _, _, _ = _make_quota(cooldown_remaining=60.0)
    reasons.add(GroqAgent(q_cool).bid(LLMContext(prompt="x", purpose="cleaner")).reason)

    # tpm_high
    q_hot, _, _, _ = _make_quota(tpm_used=5000)
    reasons.add(GroqAgent(q_hot).bid(LLMContext(prompt="x", purpose="cleaner")).reason)

    assert len(reasons) == 3, f"預期 3 個 distinct reason，實際: {reasons}"


def test_bid_dense_zero_when_endpoint_missing():
    from llm_agents.base import LLMContext
    from llm_agents.groq_agent import GroqAgent
    agent = GroqAgent(QuotaService([]))  # 沒任何 endpoint
    bid = agent.bid(LLMContext(prompt="x", purpose="cleaner"))
    assert bid.confidence == 0.0
    assert "not_registered" in bid.reason or "missing" in bid.reason.lower()


# ---------------------------------------------------------------------------
# 4. Happy path — bid 數值合理
# ---------------------------------------------------------------------------

def test_bid_happy_path_confidence_above_threshold():
    from llm_agents.base import LLMContext
    from llm_agents.groq_agent import GroqAgent
    quota, _, _, _ = _make_quota()
    agent = GroqAgent(quota)
    bid = agent.bid(LLMContext(prompt="x", purpose="cleaner"))
    assert bid.confidence >= 0.30, "happy path 該過 MIN_CONFIDENCE 門檻"
    assert bid.provider == "groq"
    assert bid.estimated_latency_ms > 0


def test_bid_confidence_decays_as_tpm_pressure_increases():
    """TPM 用得越多，confidence 該降（但還沒到 headroom 不應 dense 0.0）."""
    from llm_agents.base import LLMContext
    from llm_agents.groq_agent import GroqAgent
    quota_low, _, _, _ = _make_quota(tpm_used=0)
    quota_mid, _, _, _ = _make_quota(tpm_used=3000)  # 50% used, 還沒到 4500 headroom
    ctx = LLMContext(prompt="x", purpose="cleaner")
    bid_low = GroqAgent(quota_low).bid(ctx)
    bid_mid = GroqAgent(quota_mid).bid(ctx)
    assert bid_mid.confidence < bid_low.confidence, "TPM 壓力越大 confidence 該越低"


# ---------------------------------------------------------------------------
# 5. Handler integration — async handle 真打 client
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_calls_groq_client_and_records_usage():
    from llm_agents.base import LLMContext
    from llm_agents.groq_agent import GroqAgent
    quota, ep_q, _, _ = _make_quota()
    ep_q.client.chat.completions.create = AsyncMock(
        return_value=_make_chat_response("groq says hi", total_tokens=120))
    agent = GroqAgent(quota)
    result = await agent.handle(LLMContext(prompt="say hi", purpose="cleaner"))
    assert result == "groq says hi"
    ep_q.client.chat.completions.create.assert_awaited_once()
    # 確認 record_usage 跑了（TPM 用量該 = 120）
    assert quota.state("groq-quick").tpm_used == 120


@pytest.mark.asyncio
async def test_handle_uses_correct_model_per_min_quality():
    from llm_agents.base import LLMContext
    from llm_agents.groq_agent import GroqAgent
    quota, ep_q, ep_a, _ = _make_quota()
    ep_q.client.chat.completions.create = AsyncMock(return_value=_make_chat_response())
    ep_a.client.chat.completions.create = AsyncMock(return_value=_make_chat_response())

    agent = GroqAgent(quota)
    await agent.handle(LLMContext(prompt="x", purpose="marvin_chat", min_quality="high"))
    # 70b endpoint 該被呼叫，8b 不該
    ep_a.client.chat.completions.create.assert_awaited_once()
    ep_q.client.chat.completions.create.assert_not_awaited()
    # model 該是 70b
    kwargs = ep_a.client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "llama-3.3-70b-versatile"


@pytest.mark.asyncio
async def test_handle_marks_429_on_rate_limit_error():
    """API 拋 rate-limit 例外 → mark_429 forward → 該 endpoint 變 unavailable."""
    from llm_agents.base import LLMContext
    from llm_agents.groq_agent import GroqAgent
    quota, ep_q, _, _ = _make_quota()
    ep_q.client.chat.completions.create = AsyncMock(
        side_effect=RuntimeError("429 rate limit, try again in 30s"))
    agent = GroqAgent(quota)
    with pytest.raises(Exception):
        await agent.handle(LLMContext(prompt="x", purpose="cleaner"))
    # 該 endpoint 該進冷卻
    state = quota.state("groq-quick")
    assert state.available is False
    assert state.cooldown_remaining_s > 0


# ---------------------------------------------------------------------------
# F5 regression — typo purpose 不擋 agent bid
# ---------------------------------------------------------------------------

def test_bid_works_for_typo_purpose():
    """F5 — purpose 拼錯（"marvine_chat"）agent 還是該給合理 bid，不爆。Warning 在 LLMBus 層處理。"""
    from llm_agents.base import LLMContext
    from llm_agents.groq_agent import GroqAgent
    quota, _, _, _ = _make_quota()
    agent = GroqAgent(quota)
    bid = agent.bid(LLMContext(prompt="x", purpose="marvine_chat"))  # typo
    assert bid.confidence > 0.0
    assert bid.provider == "groq"
