"""TDD — Plan C8: LLMBus multi-agent behavior (Groq + Cerebras 同 bus).

範圍：bus 層在多 agent 共存時的行為（單 agent 已在 test_groq/test_cerebras 覆蓋；
bus 機制已在 test_llm_agents_base 用 MagicMock 覆蓋。本檔聚焦 **real agents + real
QuotaService 跨 agent 互動**）。

關鍵 invariant:
- agent A 進冷卻 → agent B 仍正常 bid（cooldown 不擴散）
- 全 agent 冷卻 → NoLLMAvailable
- agent 冷卻過期 → 自動重新進入 bid 候選
- F4 stickiness 跨 agent 邊界 — 上次贏家 +0.10 bonus 該翻盤
- F5 unknown purpose multi-agent 下不擋 dispatch
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from llm_agents.base import LLMBus, LLMContext, NoLLMAvailable
from llm_agents.cerebras_agent import CerebrasAgent
from llm_agents.groq_agent import GroqAgent
from llm_agents.quota_service import QuotaService
from llm_pool import CooldownAwarePool, PoolEndpoint


# ---------------------------------------------------------------------------
# Fixture helper — build dual-agent bus with controllable quota
# ---------------------------------------------------------------------------

def _build_dual_bus():
    clock_holder = [1000.0]
    clock = lambda: clock_holder[0]

    g_q = PoolEndpoint(name="groq-quick", client=MagicMock(), model="llama-3.1-8b-instant", tpm_budget=6000)
    g_a = PoolEndpoint(name="groq-analyze", client=MagicMock(), model="llama-3.3-70b-versatile", tpm_budget=6000)
    c_q = PoolEndpoint(name="cerebras-quick", client=MagicMock(), model="llama3.1-8b", tpm_budget=60000)
    c_a = PoolEndpoint(name="cerebras-analyze", client=MagicMock(), model="qwen-3-235b-a22b-instruct-2507", tpm_budget=60000)

    g_pool = CooldownAwarePool([g_q, g_a], clock=clock)
    c_pool = CooldownAwarePool([c_q, c_a], clock=clock)
    quota = QuotaService([g_pool, c_pool])
    bus = LLMBus([GroqAgent(quota), CerebrasAgent(quota)])
    return bus, quota, clock_holder, {"g_q": g_q, "g_a": g_a, "c_q": c_q, "c_a": c_a}


def _mock_resp(content="ok", total_tokens=50):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    resp.usage = MagicMock(total_tokens=total_tokens)
    return resp


# ---------------------------------------------------------------------------
# 1. Cooldown 不擴散：Groq 冷卻 → Cerebras 仍正常
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_groq_cooldown_does_not_affect_cerebras():
    bus, quota, _, eps = _build_dual_bus()
    quota.mark_429("groq-quick", retry_after=60.0)
    eps["c_q"].client.chat.completions.create = AsyncMock(return_value=_mock_resp("from_cerebras"))

    result = await bus.dispatch(LLMContext(prompt="x", purpose="cleaner"))
    assert result == "from_cerebras"
    eps["c_q"].client.chat.completions.create.assert_awaited_once()
    eps["g_q"].client.chat.completions.create.assert_not_called()


# ---------------------------------------------------------------------------
# 2. 全 agent 冷卻 → NoLLMAvailable
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_agents_cooldown_raises_no_llm_available():
    bus, quota, _, eps = _build_dual_bus()
    quota.mark_429("groq-quick", retry_after=60.0)
    quota.mark_429("groq-analyze", retry_after=60.0)
    quota.mark_429("cerebras-quick", retry_after=60.0)
    quota.mark_429("cerebras-analyze", retry_after=60.0)

    with pytest.raises(NoLLMAvailable):
        await bus.dispatch(LLMContext(prompt="x", purpose="cleaner"))


# ---------------------------------------------------------------------------
# 3. 冷卻過期 → 自動回到 bid 候選
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agent_recovers_from_cooldown_automatically():
    bus, quota, clock_holder, eps = _build_dual_bus()
    eps["g_q"].client.chat.completions.create = AsyncMock(return_value=_mock_resp("groq_resp"))
    eps["c_q"].client.chat.completions.create = AsyncMock(return_value=_mock_resp("cerebras_resp"))

    # Groq 冷卻 30s, Cerebras 冷卻 60s
    quota.mark_429("groq-quick", retry_after=30.0)
    quota.mark_429("groq-analyze", retry_after=30.0)
    quota.mark_429("cerebras-quick", retry_after=60.0)
    quota.mark_429("cerebras-analyze", retry_after=60.0)

    # 跳 31 秒 — Groq 過期，Cerebras 還冷
    clock_holder[0] += 31.0
    result = await bus.dispatch(LLMContext(prompt="x", purpose="cleaner"))
    assert result == "groq_resp", "Groq 冷卻過期該重新可用"


# Stickiness 機制在 test_llm_agents_base.py 用 MagicMock 精確驗證；real-agent 跨 boundary
# 不重測（避免數學依賴 mock response token leak 而 flaky）。

# ---------------------------------------------------------------------------
# 4. handle 拋 429 → 該 endpoint cooldown，下一次 dispatch 自動跳到另一 agent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_429_marks_cooldown_next_dispatch_uses_other_agent():
    bus, quota, _, eps = _build_dual_bus()
    # Cerebras 第一次拋 429
    eps["c_q"].client.chat.completions.create = AsyncMock(
        side_effect=RuntimeError("429 rate limit"))
    eps["g_q"].client.chat.completions.create = AsyncMock(return_value=_mock_resp("from_groq"))

    # 第一次：bus 選 Cerebras (headroom 大) → handle 拋例外 → re-raise
    with pytest.raises(RuntimeError):
        await bus.dispatch(LLMContext(prompt="x", purpose="cleaner"))
    # 該 endpoint 進冷卻
    assert quota.state("cerebras-quick").cooldown_remaining_s > 0

    # 第二次：Cerebras 冷卻中 → Groq 接走
    result = await bus.dispatch(LLMContext(prompt="x", purpose="cleaner"))
    assert result == "from_groq"


# ---------------------------------------------------------------------------
# 6. F5 unknown purpose 多 agent 下不擋 dispatch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_purpose_multi_agent_still_dispatches(caplog):
    import logging
    bus, quota, _, eps = _build_dual_bus()
    eps["c_q"].client.chat.completions.create = AsyncMock(return_value=_mock_resp("ok"))
    with caplog.at_level(logging.WARNING, logger="MarvinBot.LLMBus"):
        result = await bus.dispatch(LLMContext(prompt="x", purpose="never_seen_purpose"))
    assert result == "ok"
    # warning 該留紀錄
    assert any("unknown purpose" in r.message.lower() for r in caplog.records)
