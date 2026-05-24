"""TDD — Plan B: bot 啟動時建立 LLMBus（Phase 1 dormant-ready wiring）.

GeminiRouter.__init__ 結尾呼叫 _init_llm_bus()，把 build_tier_pools 出來的
pools 包成 QuotaService + 對應 agents + LLMBus，存到 self._llm_bus。
無 GROQ_API_KEY → 沒 endpoint → bus = None（_call_llm wrapper 安全 degradation 走 legacy）。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from llm_pool import CooldownAwarePool, PoolEndpoint


def _make_router_mixin():
    """Minimal mixin instance for testing init method in isolation."""
    from gemini_router_llm import GeminiRouterLLMMixin
    obj = GeminiRouterLLMMixin.__new__(GeminiRouterLLMMixin)
    obj._llm_bus = None
    return obj


def _make_pools_with_groq():
    """Return (quick_pool, analyze_pool) like build_tier_pools would, with groq endpoints."""
    clock = lambda: 1000.0
    ep_q = PoolEndpoint(name="groq-quick", client=MagicMock(), model="llama-3.1-8b-instant", tpm_budget=6000)
    ep_a = PoolEndpoint(name="groq-analyze", client=MagicMock(), model="llama-3.3-70b-versatile", tpm_budget=6000)
    return CooldownAwarePool([ep_q], clock=clock), CooldownAwarePool([ep_a], clock=clock)


def _make_empty_pools():
    """No-provider case (没 API key)."""
    clock = lambda: 1000.0
    return CooldownAwarePool([], clock=clock), CooldownAwarePool([], clock=clock)


# ---------------------------------------------------------------------------
# Happy path: Groq 有 key → bus with GroqAgent
# ---------------------------------------------------------------------------

def test_init_llm_bus_creates_bus_with_groq_agent_when_groq_endpoint_present():
    from llm_agents.base import LLMBus
    from llm_agents.groq_agent import GroqAgent
    obj = _make_router_mixin()
    obj._init_llm_bus(pools=_make_pools_with_groq())
    assert obj._llm_bus is not None
    assert isinstance(obj._llm_bus, LLMBus)
    assert any(isinstance(a, GroqAgent) for a in obj._llm_bus._agents), \
        "GroqAgent 該被加進 bus"


def test_init_llm_bus_indexes_groq_endpoints_in_quota_service():
    """Bus 內 GroqAgent 該能用 quota.state('groq-quick') 拿到 endpoint."""
    obj = _make_router_mixin()
    obj._init_llm_bus(pools=_make_pools_with_groq())
    bus = obj._llm_bus
    groq_agent = bus._agents[0]
    state = groq_agent.quota.state("groq-quick")
    assert state is not None
    assert state.model == "llama-3.1-8b-instant"


# ---------------------------------------------------------------------------
# Edge case: empty pools → no agents → bus = None
# ---------------------------------------------------------------------------

def test_init_llm_bus_none_when_no_provider_keys():
    """Phase 1 only checks Groq; provider key 全缺 → 沒 agent → bus 設 None
    (_call_llm wrapper safe degradation 跑 legacy)."""
    obj = _make_router_mixin()
    obj._init_llm_bus(pools=_make_empty_pools())
    assert obj._llm_bus is None


# ---------------------------------------------------------------------------
# Edge case: build_tier_pools raise → bus = None (不該整個 init 死)
# ---------------------------------------------------------------------------

def test_init_llm_bus_handles_build_failure_gracefully():
    """env 怪 / openai SDK 缺 / build_tier_pools 拋例外 → bus=None，bot 還能繼續啟動。"""
    obj = _make_router_mixin()

    def broken_factory():
        raise RuntimeError("simulated env / SDK 缺失")

    obj._init_llm_bus(pool_factory=broken_factory)
    assert obj._llm_bus is None


# ---------------------------------------------------------------------------
# Integration: 預設 pool_factory 是 build_tier_pools
# ---------------------------------------------------------------------------

def test_init_llm_bus_default_factory_is_build_tier_pools(monkeypatch):
    """不傳 pools / pool_factory → 預設用 llm_pool.build_tier_pools 讀 env."""
    obj = _make_router_mixin()

    called = {"count": 0}
    def fake_factory():
        called["count"] += 1
        return _make_empty_pools()

    obj._init_llm_bus(pool_factory=fake_factory)
    assert called["count"] == 1
