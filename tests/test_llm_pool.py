"""TDD: CooldownAwarePool — 多 free-tier endpoint 當一個算力池。

設計（project_llm_tier_wrapper.md，2026-05-21 Jack 拍板）：
- 429 → 記 cooldown_until，冷卻期 next_available 直接跳過（不每次都撞）
- TPM 接近上限（budget*headroom）也跳
- next_available() 唯一入口，按註冊順序回第一個有 quota 的；全滿回 None
- parse_retry_after 從 Groq 429 訊息抽 retry-after 秒數
"""
from __future__ import annotations

import pytest

from llm_pool import CooldownAwarePool, PoolEndpoint, parse_retry_after


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
