"""TDD — QuotaService thin shim wraps llm_pool.CooldownAwarePool.

範圍 (Plan C2):
- 給 agent.bid() 用的 sync state lookup (≤5ms)
- 不重做 TPM/cooldown，把 llm_pool 既有狀態 expose
- record_usage / mark_429 forward 到底層 pool
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from llm_pool import CooldownAwarePool, PoolEndpoint


def _clock_factory():
    """Mutable clock for test control."""
    holder = [1000.0]
    def now():
        return holder[0]
    def advance(secs):
        holder[0] += secs
    return now, advance


def _make_pool(eps, clock):
    return CooldownAwarePool(eps, clock=clock)


# ---------------------------------------------------------------------------
# State lookup
# ---------------------------------------------------------------------------

def test_state_returns_none_for_unknown_endpoint():
    from llm_agents.quota_service import QuotaService
    qs = QuotaService([])
    assert qs.state("nonexistent") is None


def test_state_returns_endpoint_state_for_known_name():
    from llm_agents.quota_service import QuotaService, EndpointState
    clock, _ = _clock_factory()
    ep = PoolEndpoint(name="groq-quick", model="llama-3.1-8b", tpm_budget=6000)
    pool = _make_pool([ep], clock)
    qs = QuotaService([pool])
    state = qs.state("groq-quick")
    assert isinstance(state, EndpointState)
    assert state.name == "groq-quick"
    assert state.model == "llama-3.1-8b"
    assert state.tpm_budget == 6000


def test_state_available_when_cooled_and_low_tpm():
    from llm_agents.quota_service import QuotaService
    clock, _ = _clock_factory()
    ep = PoolEndpoint(name="groq-quick", model="m", tpm_budget=6000)
    pool = _make_pool([ep], clock)
    qs = QuotaService([pool])
    state = qs.state("groq-quick")
    assert state.available is True
    assert state.cooldown_remaining_s == 0.0
    assert state.tpm_used == 0


def test_state_unavailable_during_cooldown():
    from llm_agents.quota_service import QuotaService
    clock, advance = _clock_factory()
    ep = PoolEndpoint(name="groq-quick", model="m", tpm_budget=6000)
    pool = _make_pool([ep], clock)
    qs = QuotaService([pool])
    pool.mark_429(ep, retry_after=60.0)
    state = qs.state("groq-quick")
    assert state.available is False
    assert 59.0 < state.cooldown_remaining_s <= 60.0


def test_state_unavailable_when_tpm_above_headroom():
    """TPM_HEADROOM=0.75 → 6000*0.75=4500，>4500 算 high → 不 available."""
    from llm_agents.quota_service import QuotaService
    clock, _ = _clock_factory()
    ep = PoolEndpoint(name="groq-quick", model="m", tpm_budget=6000)
    pool = _make_pool([ep], clock)
    qs = QuotaService([pool])
    # 喂超過 headroom 的 token
    pool.record_usage(ep, 5000)
    state = qs.state("groq-quick")
    assert state.available is False
    assert state.tpm_used == 5000


def test_state_recovers_after_cooldown_expires():
    from llm_agents.quota_service import QuotaService
    clock, advance = _clock_factory()
    ep = PoolEndpoint(name="groq-quick", model="m", tpm_budget=6000)
    pool = _make_pool([ep], clock)
    qs = QuotaService([pool])
    pool.mark_429(ep, retry_after=30.0)
    assert qs.state("groq-quick").available is False
    advance(31.0)
    assert qs.state("groq-quick").available is True


# ---------------------------------------------------------------------------
# Multi-pool indexing
# ---------------------------------------------------------------------------

def test_multiple_pools_combined_index():
    """quick + analyze 兩個 pool 都丟給 QuotaService，name 不重複可以同時查。"""
    from llm_agents.quota_service import QuotaService
    clock, _ = _clock_factory()
    ep_q = PoolEndpoint(name="groq-quick", model="8b", tpm_budget=6000)
    ep_a = PoolEndpoint(name="groq-analyze", model="70b", tpm_budget=6000)
    pool_q = _make_pool([ep_q], clock)
    pool_a = _make_pool([ep_a], clock)
    qs = QuotaService([pool_q, pool_a])
    assert qs.state("groq-quick").model == "8b"
    assert qs.state("groq-analyze").model == "70b"


# ---------------------------------------------------------------------------
# Forwarding mutations
# ---------------------------------------------------------------------------

def test_record_usage_forwards_to_underlying_pool():
    from llm_agents.quota_service import QuotaService
    clock, _ = _clock_factory()
    ep = PoolEndpoint(name="groq-quick", model="m", tpm_budget=6000)
    pool = _make_pool([ep], clock)
    qs = QuotaService([pool])
    qs.record_usage("groq-quick", 1234)
    # 直接驗 underlying pool 狀態變了
    assert pool.current_tpm(ep) == 1234


def test_record_usage_unknown_endpoint_is_noop():
    """unknown name 安靜略過，不該拋例外（caller 弄錯名也只是 metric 漏記）。"""
    from llm_agents.quota_service import QuotaService
    qs = QuotaService([])
    qs.record_usage("nonexistent", 100)  # 不該炸


def test_mark_429_forwards_with_retry_after():
    from llm_agents.quota_service import QuotaService
    clock, _ = _clock_factory()
    ep = PoolEndpoint(name="groq-quick", model="m", tpm_budget=6000)
    pool = _make_pool([ep], clock)
    qs = QuotaService([pool])
    qs.mark_429("groq-quick", retry_after=45.0)
    state = qs.state("groq-quick")
    assert state.available is False
    assert 44.0 < state.cooldown_remaining_s <= 45.0


def test_mark_429_parses_retry_after_from_err_str():
    """Forward `err_str` 讓 underlying pool 從 Groq 訊息抽 retry-after。"""
    from llm_agents.quota_service import QuotaService
    clock, _ = _clock_factory()
    ep = PoolEndpoint(name="groq-quick", model="m", tpm_budget=6000)
    pool = _make_pool([ep], clock)
    qs = QuotaService([pool])
    qs.mark_429("groq-quick", err_str="Please try again in 2m30s")
    state = qs.state("groq-quick")
    # 2m30s = 150s
    assert 149.0 < state.cooldown_remaining_s <= 150.0


def test_mark_429_unknown_endpoint_is_noop():
    from llm_agents.quota_service import QuotaService
    qs = QuotaService([])
    qs.mark_429("nonexistent", retry_after=10.0)  # 不該炸


# ---------------------------------------------------------------------------
# TPM ratio convenience
# ---------------------------------------------------------------------------

def test_state_tpm_ratio_reflects_usage():
    """tpm_used / tpm_budget — agent bid 用來算 confidence 衰減。"""
    from llm_agents.quota_service import QuotaService
    clock, _ = _clock_factory()
    ep = PoolEndpoint(name="groq-quick", model="m", tpm_budget=6000)
    pool = _make_pool([ep], clock)
    qs = QuotaService([pool])
    qs.record_usage("groq-quick", 3000)
    state = qs.state("groq-quick")
    assert abs(state.tpm_ratio - 0.5) < 1e-6, f"3000/6000 = 0.5，實際: {state.tpm_ratio}"


def test_state_tpm_ratio_zero_when_unused():
    from llm_agents.quota_service import QuotaService
    clock, _ = _clock_factory()
    ep = PoolEndpoint(name="groq-quick", model="m", tpm_budget=6000)
    pool = _make_pool([ep], clock)
    qs = QuotaService([pool])
    assert qs.state("groq-quick").tpm_ratio == 0.0
