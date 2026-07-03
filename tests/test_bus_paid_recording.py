"""TDD: bus 付費尾（gemini_paid）記帳 + guard 強制（2026-07-03 帳務調查）。

洞：bus 是 OpenAI-compat 池，gemini_paid 端點的呼叫完全不進
llm_paid_usage.jsonl 也不過 PaidUsageGuard——「$0.5/天硬上限」對 bus
流量咬不住；免費池晚高峰全崩時流量倒進付費尾，帳單看得到、記帳看不到。

修：TieredLLMRouter._chat 的 _call 內——
  R1 paid 端點成功 → _record_paid_usage（usage.prompt/completion_tokens）
  R2 免費端點 → 不記 paid 帳
  R3 paid 端點呼叫前過 guard.allow()，超上限 → 不打 API、讓位
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from llm_pool import TieredLLMRouter, CooldownAwarePool, PoolEndpoint
from llm_paid import PaidUsageGuard


class _FakeClient:
    def __init__(self, content="好", prompt_tokens=100, completion_tokens=50):
        self.calls = 0
        self._resp = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(total_tokens=prompt_tokens + completion_tokens,
                                  prompt_tokens=prompt_tokens,
                                  completion_tokens=completion_tokens),
        )
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls += 1
        return self._resp


def _router(ep_name, client, tmp_path, daily_cap=5.0):
    ep = PoolEndpoint(name=ep_name, client=client, model="gemini-2.5-flash")
    pool = CooldownAwarePool([ep])
    guard = PaidUsageGuard(log_path=tmp_path / "paid.jsonl",
                           daily_cap_usd=daily_cap, monthly_cap_usd=100.0)
    return TieredLLMRouter(pool, pool, paid_guard=guard), guard


def _rows(tmp_path):
    p = tmp_path / "paid.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


@pytest.mark.asyncio
async def test_paid_endpoint_records_usage(tmp_path):
    client = _FakeClient(prompt_tokens=200, completion_tokens=80)
    router, _ = _router("gemini_paid", client, tmp_path)
    out = await router.quick("測試", caller="cleaner_rescue")
    assert out == "好"
    rows = _rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["caller"] == "cleaner_rescue"
    assert rows[0]["tokens"] == 280
    assert rows[0]["est_usd"] > 0


@pytest.mark.asyncio
async def test_free_endpoint_not_recorded(tmp_path):
    client = _FakeClient()
    router, _ = _router("groq", client, tmp_path)
    out = await router.quick("測試", caller="cleaner")
    assert out == "好"
    assert _rows(tmp_path) == []


@pytest.mark.asyncio
async def test_paid_endpoint_blocked_when_over_cap(tmp_path):
    """超 guard 上限 → 不打 API（client.calls==0）、dispatch 讓位回 None。"""
    client = _FakeClient()
    router, guard = _router("gemini_paid", client, tmp_path, daily_cap=0.001)
    guard.record(caller="x", model="gemini-2.5-flash", tokens=99999, est_usd=0.5)  # 先塞爆
    out = await router.quick("測試", caller="cleaner_rescue", max_tokens=500)
    assert out is None
    assert client.calls == 0
