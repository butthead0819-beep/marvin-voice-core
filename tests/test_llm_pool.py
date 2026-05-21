"""TDD: CooldownAwarePool — 多 free-tier endpoint 當一個算力池。

設計（project_llm_tier_wrapper.md，2026-05-21 Jack 拍板）：
- 429 → 記 cooldown_until，冷卻期 next_available 直接跳過（不每次都撞）
- TPM 接近上限（budget*headroom）也跳
- next_available() 唯一入口，按註冊順序回第一個有 quota 的；全滿回 None
- parse_retry_after 從 Groq 429 訊息抽 retry-after 秒數
"""
from __future__ import annotations

import pytest

from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

from llm_pool import (
    CooldownAwarePool, PoolEndpoint, TieredLLMRouter,
    dispatch, is_rate_limit, parse_retry_after,
)


# ── parse_retry_after ────────────────────────────────────────────────────────

def test_parse_minutes_and_seconds():
    # Groq 真實訊息：'Please try again in 4m18.8544s'
    assert parse_retry_after("Please try again in 4m18.8544s") == pytest.approx(258.85, abs=0.1)


def test_parse_seconds_only():
    assert parse_retry_after("try again in 30s") == 30.0


def test_parse_minutes_only():
    assert parse_retry_after("try again in 1m") == 60.0


def test_parse_garbage_returns_default():
    assert parse_retry_after("some unrelated error") == 30.0
    assert parse_retry_after("") == 30.0


# ── next_available ───────────────────────────────────────────────────────────

class _Clock:
    def __init__(self, t=1000.0):
        self.t = t
    def __call__(self):
        return self.t


def _pool(clock, *endpoints):
    return CooldownAwarePool(list(endpoints), clock=clock)


def test_returns_first_in_priority_order():
    clk = _Clock()
    a = PoolEndpoint(name="groq-8b")
    b = PoolEndpoint(name="cerebras-8b")
    pool = _pool(clk, a, b)
    assert pool.next_available() is a


def test_skips_endpoint_in_cooldown():
    clk = _Clock()
    a = PoolEndpoint(name="groq-8b")
    b = PoolEndpoint(name="cerebras-8b")
    pool = _pool(clk, a, b)
    pool.mark_429(a, retry_after=60)
    assert pool.next_available() is b


def test_all_in_cooldown_returns_none():
    clk = _Clock()
    a = PoolEndpoint(name="a")
    b = PoolEndpoint(name="b")
    pool = _pool(clk, a, b)
    pool.mark_429(a, retry_after=60)
    pool.mark_429(b, retry_after=60)
    assert pool.next_available() is None


def test_cooldown_expires_then_available_again():
    clk = _Clock()
    a = PoolEndpoint(name="a")
    pool = _pool(clk, a)
    pool.mark_429(a, retry_after=30)
    assert pool.next_available() is None
    clk.t += 31              # 冷卻過了
    assert pool.next_available() is a


def test_mark_429_parses_retry_after_from_errstr():
    clk = _Clock()
    a = PoolEndpoint(name="a")
    pool = _pool(clk, a)
    pool.mark_429(a, "Rate limit ... Please try again in 1m")
    assert a.cooldown_until == pytest.approx(1000.0 + 60.0)


# ── TPM headroom ─────────────────────────────────────────────────────────────

def test_skips_endpoint_near_tpm_budget():
    clk = _Clock()
    a = PoolEndpoint(name="a", tpm_budget=6000)
    b = PoolEndpoint(name="b", tpm_budget=6000)
    pool = _pool(clk, a, b)
    pool.record_usage(a, 5000)   # > 6000*0.75=4500 → 跳
    assert pool.next_available() is b


def test_tpm_window_rolls_off_after_60s():
    clk = _Clock()
    a = PoolEndpoint(name="a", tpm_budget=6000)
    pool = _pool(clk, a)
    pool.record_usage(a, 5000)
    assert pool.next_available() is None     # 近上限
    clk.t += 61                              # 滾出 60s 視窗
    assert pool.next_available() is a
    assert pool.current_tpm(a) == 0


# ── is_rate_limit ────────────────────────────────────────────────────────────

def test_is_rate_limit_detects_429_and_quota():
    assert is_rate_limit(Exception("Error code: 429 - rate_limit_exceeded"))
    assert is_rate_limit(Exception("tokens per day (TPD): Limit 500000"))
    assert is_rate_limit(Exception("Too Many Requests"))


def test_is_rate_limit_false_for_other_errors():
    assert not is_rate_limit(Exception("connection timeout"))
    assert not is_rate_limit(Exception("500 internal error"))


# ── dispatch ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_returns_result_and_records_usage():
    clk = _Clock()
    a = PoolEndpoint(name="a", tpm_budget=6000)
    pool = _pool(clk, a)

    async def call_fn(ep):
        return "hello", 120

    out = await dispatch(pool, call_fn)
    assert out == "hello"
    assert pool.current_tpm(a) == 120


@pytest.mark.asyncio
async def test_dispatch_rate_limit_marks_and_tries_next():
    clk = _Clock()
    a = PoolEndpoint(name="a")
    b = PoolEndpoint(name="b")
    pool = _pool(clk, a, b)

    async def call_fn(ep):
        if ep is a:
            raise Exception("Error code: 429 rate limit")
        return "from-b", 50

    out = await dispatch(pool, call_fn)
    assert out == "from-b"
    assert a.cooldown_until > clk.t       # a 被冷卻
    assert pool.current_tpm(b) == 50


@pytest.mark.asyncio
async def test_dispatch_transient_error_short_cooldown_tries_next():
    clk = _Clock()
    a = PoolEndpoint(name="a")
    b = PoolEndpoint(name="b")
    pool = _pool(clk, a, b)

    async def call_fn(ep):
        if ep is a:
            raise Exception("connection timeout")   # 非 429
        return "ok", 10

    out = await dispatch(pool, call_fn)
    assert out == "ok"
    # 非 429 → 短冷卻（5s），不是長 retry-after
    assert a.cooldown_until == pytest.approx(clk.t + 5.0)


@pytest.mark.asyncio
async def test_dispatch_all_fail_returns_none():
    clk = _Clock()
    a = PoolEndpoint(name="a")
    b = PoolEndpoint(name="b")
    pool = _pool(clk, a, b)

    async def call_fn(ep):
        raise Exception("429")

    assert await dispatch(pool, call_fn) is None    # 兩個都冷卻 → None


@pytest.mark.asyncio
async def test_dispatch_empty_pool_returns_none():
    clk = _Clock()
    pool = _pool(clk)

    async def call_fn(ep):
        return "x", 1

    assert await dispatch(pool, call_fn) is None


# ── TieredLLMRouter ──────────────────────────────────────────────────────────

def _fake_ep(name, content, tokens=42):
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(total_tokens=tokens),
    )
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=resp)
    return PoolEndpoint(name=name, client=client, model=f"{name}-model")


def _fake_ep_429(name):
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=Exception("Error 429 rate limit"))
    return PoolEndpoint(name=name, client=client, model=f"{name}-model")


@pytest.mark.asyncio
async def test_quick_uses_quick_pool_and_attributes_caller():
    clk = _Clock()
    ep = _fake_ep("groq-8b", "cleaned text", tokens=80)
    router = TieredLLMRouter(CooldownAwarePool([ep], clock=clk),
                             CooldownAwarePool([], clock=clk))

    out = await router.quick("clean this", caller="stt_cleaner")

    assert out == "cleaned text"
    assert router.usage_by_caller["stt_cleaner"] == 80
    # OpenAI 相容呼叫帶對 model
    assert ep.client.chat.completions.create.await_args.kwargs["model"] == "groq-8b-model"


@pytest.mark.asyncio
async def test_quick_failover_on_429_to_next_endpoint():
    clk = _Clock()
    bad = _fake_ep_429("groq-8b")
    good = _fake_ep("cerebras-8b", "from cerebras", tokens=60)
    router = TieredLLMRouter(CooldownAwarePool([bad, good], clock=clk),
                             CooldownAwarePool([], clock=clk))

    out = await router.quick("hi", caller="ack")

    assert out == "from cerebras"
    assert bad.cooldown_until > clk.t
    assert router.usage_by_caller["ack"] == 60


@pytest.mark.asyncio
async def test_analyze_json_mode_sets_response_format():
    clk = _Clock()
    ep = _fake_ep("groq-70b", '{"x":1}')
    router = TieredLLMRouter(CooldownAwarePool([], clock=clk),
                             CooldownAwarePool([ep], clock=clk))

    await router.analyze("analyze", caller="resolver", json=True)

    kwargs = ep.client.chat.completions.create.await_args.kwargs
    assert kwargs["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_quick_all_exhausted_returns_none():
    clk = _Clock()
    bad = _fake_ep_429("groq-8b")
    router = TieredLLMRouter(CooldownAwarePool([bad], clock=clk),
                             CooldownAwarePool([], clock=clk))

    assert await router.quick("hi", caller="x") is None
