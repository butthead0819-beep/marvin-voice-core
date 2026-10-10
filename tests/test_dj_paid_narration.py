"""TDD — DJ 口白改走付費 gemini-3.1-flash-lite（記帳+guard），失敗退回免費 bus。

_call_paid_gemini 抽自原本 _call_cloud 的「💰 [Paid Fallback]」段落：先過
PaidUsageGuard、成功記帳；沒有 paid client / 超 cap → raise RuntimeError。
generate_dynamic_system_msg 的 dj_interjection 分支優先打它，失敗（任何
Exception）退回免費 bus（_call_llm + DJ_PREFER_PROVIDERS）。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gemini_router_content import DJ_PREFER_PROVIDERS, GeminiRouterContentMixin
from llm_paid import PaidUsageGuard


def _make_mixin():
    inst = GeminiRouterContentMixin.__new__(GeminiRouterContentMixin)
    inst.dna = {}
    inst._dyn_msg_cache = False  # 繞過快取，確保一定會走生成路徑
    inst._call_llm = AsyncMock(return_value="唉...")
    return inst


@pytest.mark.asyncio
async def test_dj_interjection_uses_paid_gemini_first():
    mixin = _make_mixin()
    mixin._call_paid_gemini = AsyncMock(return_value="付費台詞")

    result = await mixin.generate_dynamic_system_msg("dj_interjection", context="周杰倫 七里香")

    assert result == "付費台詞"
    mixin._call_llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_dj_interjection_falls_back_to_bus_on_paid_failure():
    mixin = _make_mixin()
    mixin._call_paid_gemini = AsyncMock(side_effect=RuntimeError("paid_cap_exceeded"))

    result = await mixin.generate_dynamic_system_msg("dj_interjection", context="周杰倫 七里香")

    assert result == "唉..."
    mixin._call_llm.assert_awaited_once()
    _, kwargs = mixin._call_llm.call_args
    assert kwargs.get("prefer_providers") == DJ_PREFER_PROVIDERS


@pytest.mark.asyncio
async def test_non_dj_event_does_not_call_paid_gemini():
    mixin = _make_mixin()
    mixin._call_paid_gemini = AsyncMock(return_value="付費台詞")

    await mixin.generate_dynamic_system_msg("radio_now_playing", context="周杰倫 七里香")

    mixin._call_paid_gemini.assert_not_awaited()


# ── _call_paid_gemini helper 單元測試 ────────────────────────────────────────

def _usage(prompt_tokens=100, out_tokens=40):
    return SimpleNamespace(total_token_count=prompt_tokens + out_tokens,
                           prompt_token_count=prompt_tokens,
                           candidates_token_count=out_tokens)


def _fake_paid_client(text="付費回覆", usage=None):
    resp = SimpleNamespace(text=text, usage_metadata=usage if usage is not None else _usage())

    async def _generate_content(**kwargs):
        return resp

    client = SimpleNamespace()
    client.aio = SimpleNamespace(models=SimpleNamespace(generate_content=_generate_content))
    return client, resp


def _make_router(tmp_path, paid_client=None, daily_cap=5.0):
    from gemini_router_llm import GeminiRouterLLMMixin
    r = GeminiRouterLLMMixin.__new__(GeminiRouterLLMMixin)
    r.provider = "gemini"
    r.cleaner_model = "gemini-3.1-flash-lite-preview"
    r.google_paid_client = paid_client
    r.budget = MagicMock()
    r._acquire_cloud_rpm_slot = AsyncMock(return_value=None)
    guard = PaidUsageGuard(log_path=tmp_path / "paid.jsonl",
                           daily_cap_usd=daily_cap, monthly_cap_usd=100.0)
    r._get_paid_guard = MagicMock(return_value=guard)
    return r, guard


def _rows(tmp_path):
    p = tmp_path / "paid.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


@pytest.mark.asyncio
async def test_call_paid_gemini_records_usage(tmp_path):
    client, _ = _fake_paid_client()
    router, _guard = _make_router(tmp_path, paid_client=client)

    result = await router._call_paid_gemini("系統", "動態台詞生成", caller="dj_narration")

    assert result == "付費回覆"
    rows = _rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["caller"] == "dj_narration"


@pytest.mark.asyncio
async def test_call_paid_gemini_over_cap_raises_without_calling_api(tmp_path):
    client, resp = _fake_paid_client()
    router, guard = _make_router(tmp_path, paid_client=client, daily_cap=0.001)
    guard.record(caller="x", model="gemini-3.1-flash-lite-preview", tokens=99999, est_usd=0.5)

    called = {"n": 0}

    async def _spy(**kwargs):
        called["n"] += 1
        return resp
    client.aio.models.generate_content = _spy

    with pytest.raises(RuntimeError):
        await router._call_paid_gemini("系統", "動態台詞生成", caller="dj_narration")
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_call_paid_gemini_no_paid_client_raises(tmp_path):
    router, _guard = _make_router(tmp_path, paid_client=None)

    with pytest.raises(RuntimeError):
        await router._call_paid_gemini("系統", "動態台詞生成", caller="dj_narration")
