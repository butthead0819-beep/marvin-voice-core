"""① Daily budget 追蹤 + ④ degraded 告警 — 6/2 加。

① 驗：daily 用量壓 confidence、daily 近上限視為不可用、跨日重置
④ 驗：viable provider ≤ threshold → debounced ERROR log + callback
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from llm_agents.base import LLMBid, LLMBus, LLMContext, LLMAgent, NoLLMAvailable
from llm_agents.quota_service import QuotaService
from llm_pool import CooldownAwarePool, PoolEndpoint


# ── ① Daily budget ────────────────────────────────────────────────────────────

def _pool_with_daily(daily_budget, daily_used, *, reset_in=3600.0):
    clock = [1000.0]
    ep = PoolEndpoint(name="groq-quick", client=MagicMock(), model="m",
                      tpm_budget=6000, daily_budget=daily_budget,
                      daily_used=daily_used, daily_reset_at=1000.0 + reset_in)
    pool = CooldownAwarePool([ep], clock=lambda: clock[0])
    return pool, ep, clock


def test_daily_ratio_computed():
    pool, ep, _ = _pool_with_daily(100000, 80000)
    assert abs(pool.daily_ratio(ep) - 0.8) < 1e-6


def test_daily_zero_budget_no_penalty():
    pool, ep, _ = _pool_with_daily(0, 999999)
    assert pool.daily_ratio(ep) == 0.0  # budget=0 → 不罰


def test_daily_near_limit_skipped_by_next_available():
    pool, ep, _ = _pool_with_daily(100000, 95000)  # 95% > DAILY_HEADROOM 0.92
    assert pool.next_available() is None


def test_daily_under_headroom_available():
    pool, ep, _ = _pool_with_daily(100000, 50000)  # 50% < headroom
    assert pool.next_available() is ep


def test_record_usage_accumulates_daily():
    pool, ep, clock = _pool_with_daily(100000, 0)
    pool.record_usage(ep, 1000)
    pool.record_usage(ep, 500)
    assert ep.daily_used == 1500


def test_record_usage_resets_daily_after_window():
    pool, ep, clock = _pool_with_daily(100000, 90000, reset_in=10.0)
    clock[0] += 20.0  # 過了 daily_reset_at
    pool.record_usage(ep, 100)
    assert ep.daily_used == 100  # 重置後只剩這次


def test_quota_state_exposes_daily_ratio():
    pool, ep, _ = _pool_with_daily(100000, 60000)
    quota = QuotaService([pool])
    st = quota.state("groq-quick")
    assert abs(st.daily_ratio - 0.6) < 1e-6
    assert st.available is True  # 60% < headroom


def test_quota_state_daily_exhausted_not_available():
    pool, ep, _ = _pool_with_daily(100000, 95000)
    quota = QuotaService([pool])
    st = quota.state("groq-quick")
    assert st.available is False  # daily 95% > headroom


def test_groq_agent_bid_drops_confidence_on_high_daily():
    """daily_ratio 高 → confidence 被壓（即使 per-minute TPM 是 0）。"""
    from llm_agents.groq_agent import GroqAgent
    # daily 80%、TPM 0 → pressure=max(0, 0.8)=0.8 → conf = 0.65 - 0.8*0.30 = 0.41
    pool, ep, _ = _pool_with_daily(100000, 80000)
    quota = QuotaService([pool])
    bid = GroqAgent(quota).bid(LLMContext(prompt="x", purpose="cleaner", min_quality="fast"))
    assert bid.confidence < 0.45  # 被 daily 壓下來
    assert bid.confidence >= 0.30  # 仍在 floor 以上


# ── ④ Degraded 告警 ───────────────────────────────────────────────────────────

def _agent(name, conf, provider=None):
    a = MagicMock(spec=LLMAgent)
    a.name = name
    a.priority = 10
    a.purpose_compatible = frozenset()
    a.bid = MagicMock(return_value=LLMBid(conf, provider or name, "m", 100, 10, "r"))
    return a


@pytest.mark.asyncio
async def test_degraded_alert_fires_when_one_viable(caplog):
    """只有 1 個 viable provider → degraded ERROR log + callback。"""
    calls = []
    bus = LLMBus([_agent("groq", 0.6), _agent("cerebras", 0.0)],
                 on_degraded=lambda n, s: calls.append((n, s)))
    bus._SHORT_CIRCUIT_AFTER = 5
    bus.last_dispatch = None
    # winner 仍能跑（groq 0.6 viable）；但只有 1 個 viable → 告警
    with caplog.at_level(logging.ERROR):
        # handle 需 awaitable
        bus._agents[0].handle = _amock("ok")
        await bus.dispatch(LLMContext(prompt="x", purpose="cleaner"))
    assert len(calls) == 1
    assert calls[0][0] == 1
    assert any("DEGRADED" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_degraded_alert_debounced(caplog):
    """5 分鐘內第二次 degraded 不重複告警。"""
    calls = []
    bus = LLMBus([_agent("groq", 0.6)], on_degraded=lambda n, s: calls.append(1))
    bus._SHORT_CIRCUIT_AFTER = 5
    bus._agents[0].handle = _amock("ok")
    await bus.dispatch(LLMContext(prompt="x", purpose="cleaner"))
    await bus.dispatch(LLMContext(prompt="y", purpose="cleaner"))
    assert len(calls) == 1  # debounce → 只告警一次


@pytest.mark.asyncio
async def test_no_degraded_when_multiple_providers(caplog):
    """≥2 viable provider → 不告警。"""
    calls = []
    bus = LLMBus([_agent("groq", 0.6), _agent("cerebras", 0.55)],
                 on_degraded=lambda n, s: calls.append(1))
    bus._SHORT_CIRCUIT_AFTER = 5
    bus._agents[0].handle = _amock("ok")
    bus._agents[1].handle = _amock("ok")
    await bus.dispatch(LLMContext(prompt="x", purpose="cleaner"))
    assert len(calls) == 0


@pytest.mark.asyncio
async def test_degraded_alert_fires_on_freshly_booted_machine(monkeypatch):
    """剛開機機器（time.monotonic() 仍 < debounce 視窗）第一次 degraded 仍須告警。

    regression：_last_degraded_ts 曾 init 為 0.0，debounce 比 now-0 < 300，
    在 monotonic 落在 300 以內的 fresh runner（如 CI 容器）會把「第一次」告警
    誤判成「300s 內重複」而 debounce 吞掉 → on_degraded 不觸發。
    init 改 -inf 後第一次必觸發，與機器 uptime 無關。
    """
    import llm_agents.base as base_mod
    # 模擬剛開機：monotonic 落在 debounce 視窗內（< _DEGRADED_DEBOUNCE_S=300）
    monkeypatch.setattr(base_mod.time, "monotonic", lambda: 42.0)

    calls = []
    bus = LLMBus([_agent("groq", 0.6)], on_degraded=lambda n, s: calls.append(1))
    bus._SHORT_CIRCUIT_AFTER = 5
    bus._agents[0].handle = _amock("ok")
    await bus.dispatch(LLMContext(prompt="x", purpose="cleaner"))
    assert len(calls) == 1  # 第一次告警不該被 fresh-boot 的小 monotonic 吞掉


def _amock(ret):
    from unittest.mock import AsyncMock
    return AsyncMock(return_value=ret)
