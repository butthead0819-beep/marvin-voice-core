"""call_paid_review 先過守門 + daily_review 專用額度（fix/paid-review-guard）。

llm_pool.call_paid_review 過去只呼叫 _record_paid_usage 記帳，從不問
PaidUsageGuard.allow()，$4/月上限形同虛設。本檔驗證：
1. PaidUsageGuard 的 only_callers/exclude_callers 篩選正確。
2. guard_for_caller 依 caller 回對的 guard（daily_review 專用額度 vs 一般）。
3. call_paid_review 真的會先問 guard，guard 擋下就不打 pool、status 填 "guard_cap"；
   guard 放行才照常呼叫。

不自己複製篩選/加總邏輯驗算——全部呼叫被測函式本身。
"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest

# 檔頭直接 import：拿到的是真正的 guard_for_caller，不受 conftest 裡
# autouse fixture monkeypatch「llm_paid.guard_for_caller」屬性的影響
# （那個 patch 只影響之後才做 `from llm_paid import ...` 的 late-binding 呼叫者，
# 例如 llm_pool.call_paid_review 內部的函式內 import）。
from llm_paid import DEDICATED_QUOTAS, PaidUsageGuard, guard_for_caller
from llm_pool import PoolEndpoint, call_paid_review


def _write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _ledger(tmp_path, now):
    """帳本：daily_review 花 0.9、other 花 0.9，都在本月內。"""
    path = tmp_path / "ledger.jsonl"
    _write_rows(path, [
        {"ts": now, "caller": "daily_review", "model": "m", "tokens": 1, "est_usd": 0.9},
        {"ts": now, "caller": "other", "model": "m", "tokens": 1, "est_usd": 0.9},
    ])
    return path


# ── PaidUsageGuard 篩選 ───────────────────────────────────────────────────

def test_only_callers_counts_just_that_caller(tmp_path):
    now = time.time()
    guard = PaidUsageGuard(log_path=_ledger(tmp_path, now), clock=lambda: now,
                            only_callers=frozenset({"daily_review"}))
    assert guard.spent_month() == pytest.approx(0.9)


def test_exclude_callers_skips_that_caller(tmp_path):
    now = time.time()
    guard = PaidUsageGuard(log_path=_ledger(tmp_path, now), clock=lambda: now,
                            exclude_callers=frozenset({"daily_review"}))
    assert guard.spent_month() == pytest.approx(0.9)


def test_no_filter_counts_everyone_like_before(tmp_path):
    now = time.time()
    guard = PaidUsageGuard(log_path=_ledger(tmp_path, now), clock=lambda: now)
    assert guard.spent_month() == pytest.approx(1.8)


# ── guard_for_caller ──────────────────────────────────────────────────────

def test_guard_for_caller_daily_review_has_dedicated_quota():
    guard = guard_for_caller("daily_review")
    assert (guard.daily_cap_usd, guard.monthly_cap_usd) == DEDICATED_QUOTAS["daily_review"]
    assert guard.only_callers == frozenset({"daily_review"})


def test_guard_for_caller_other_caller_gets_default_quota_excluding_dedicated():
    guard = guard_for_caller("associative_curation")
    assert (guard.daily_cap_usd, guard.monthly_cap_usd) == (0.5, 4.0)
    assert "daily_review" in guard.exclude_callers


def test_guard_for_caller_isolation_real_ledger(tmp_path):
    """daily_review 當月已花 1.0（撞自己的 $1 cap）；一般 caller 當月 0 → 不受影響。"""
    now = time.time()
    path = tmp_path / "ledger.jsonl"
    _write_rows(path, [
        {"ts": now, "caller": "daily_review", "model": "m", "tokens": 1, "est_usd": 1.0},
    ])

    dr_guard = guard_for_caller("daily_review")
    dr_guard.log_path = path
    dr_guard.clock = lambda: now
    assert dr_guard.allow(0.0) is False

    other_guard = guard_for_caller("x")
    other_guard.log_path = path
    other_guard.clock = lambda: now
    assert other_guard.allow(0.0) is True


# ── call_paid_review 先過守門 ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_call_paid_review_blocked_by_guard_never_calls_pool():
    class _DenyGuard:
        def allow(self, expected_usd=0.0):
            return False

    def _unreachable(*a, **kw):
        raise AssertionError("guard 擋下後不該呼叫 generate_content")

    ep = PoolEndpoint(name="g", model="gemini-2.5-flash")
    client = MagicMock()
    client.aio.models.generate_content = _unreachable
    ep.client = client

    from llm_pool import CooldownAwarePool
    status: dict = {}
    out = await call_paid_review("content", system="sys", pool=CooldownAwarePool([ep]),
                                  guard=_DenyGuard(), status=status)
    assert out is None
    assert status["reason"] == "guard_cap"


@pytest.mark.asyncio
async def test_call_paid_review_allowed_by_guard_proceeds_normally():
    from types import SimpleNamespace

    class _AllowGuard:
        def allow(self, expected_usd=0.0):
            return True

    async def _gen(**kw):
        return SimpleNamespace(
            text='{"ok": true}',
            usage_metadata=SimpleNamespace(prompt_token_count=5, candidates_token_count=5),
        )

    ep = PoolEndpoint(name="g", model="gemini-2.5-flash")
    client = MagicMock()
    client.aio.models.generate_content = _gen
    ep.client = client

    from llm_pool import CooldownAwarePool
    out = await call_paid_review("content", system="sys", pool=CooldownAwarePool([ep]),
                                  guard=_AllowGuard())
    assert out == '{"ok": true}'
