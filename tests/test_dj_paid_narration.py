"""TDD — DJ 口白走 _call_llm(tier="premium")：付費 gemini-3.1-flash-lite（記帳+guard）優先，失敗退回免費 bus。

_call_paid_gemini 抽自原本 _call_cloud 的「💰 [Paid Fallback]」段落：先過
PaidUsageGuard、成功記帳；沒有 paid client / 超 cap → raise RuntimeError。
_call_llm(tier="premium") 先打它，失敗/空回應退回 simple 走免費 bus；
generate_dynamic_system_msg 的 dj_interjection 分支用 tier="premium"、purpose="dj_narration"。
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
async def test_dj_interjection_calls_llm_with_premium_tier():
    mixin = _make_mixin()

    result = await mixin.generate_dynamic_system_msg("dj_interjection", context="周杰倫 七里香")

    assert result == "唉..."
    _, kwargs = mixin._call_llm.call_args
    assert kwargs.get("tier") == "premium"
    assert kwargs.get("purpose") == "dj_narration"
    assert kwargs.get("prefer_providers") == DJ_PREFER_PROVIDERS


@pytest.mark.asyncio
async def test_non_dj_event_does_not_use_premium_tier():
    mixin = _make_mixin()

    await mixin.generate_dynamic_system_msg("radio_now_playing", context="周杰倫 七里香")

    _, kwargs = mixin._call_llm.call_args
    assert kwargs.get("tier") != "premium"


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


# ── _call_llm tier="premium" 與 SPOKEN 排除接線 ──────────────────────────────

class _FakeBus:
    def __init__(self, result="免費台詞"):
        self.result = result
        self.ctxs = []
        self.last_dispatch = None

    async def dispatch(self, ctx):
        self.ctxs.append(ctx)
        return self.result


def _make_llm_router(monkeypatch, tmp_path, paid_client=None):
    import llm_agents.metrics as metrics
    monkeypatch.setattr(metrics, "log_dispatch", lambda **kw: None)
    monkeypatch.setenv("LLM_BUS", "true")
    r, guard = _make_router(tmp_path, paid_client=paid_client)
    r.dna = {}
    r._llm_bus = _FakeBus()
    return r, guard


@pytest.mark.asyncio
async def test_premium_tier_uses_paid_and_skips_bus(monkeypatch, tmp_path):
    client, _ = _fake_paid_client(text="付費台詞")
    r, _ = _make_llm_router(monkeypatch, tmp_path, paid_client=client)

    out = await r._call_llm("系統", "動態台詞生成", tier="premium", purpose="dj_narration")

    assert out == "付費台詞"
    assert r._llm_bus.ctxs == []
    assert _rows(tmp_path)[0]["caller"] == "dj_narration"


@pytest.mark.asyncio
async def test_premium_tier_falls_back_to_bus_when_paid_unavailable(monkeypatch, tmp_path):
    r, _ = _make_llm_router(monkeypatch, tmp_path, paid_client=None)

    out = await r._call_llm("系統", "動態台詞生成", tier="premium", purpose="dj_narration",
                            prefer_providers=("gemini_free", "groq"))

    assert out == "免費台詞"
    ctx = r._llm_bus.ctxs[0]
    assert ctx.min_quality == "fast"           # 降回 simple
    assert ctx.prefer_providers == ("gemini_free", "groq")
    assert "mistral" in ctx.exclude_providers  # dj_narration 屬 SPOKEN_PURPOSES


@pytest.mark.asyncio
async def test_premium_tier_falls_back_when_paid_returns_empty(monkeypatch, tmp_path):
    client, _ = _fake_paid_client(text="")
    r, _ = _make_llm_router(monkeypatch, tmp_path, paid_client=client)

    out = await r._call_llm("系統", "動態台詞生成", tier="premium", purpose="dj_narration")

    assert out == "免費台詞"
    assert len(r._llm_bus.ctxs) == 1


@pytest.mark.asyncio
async def test_non_premium_tier_never_calls_paid(monkeypatch, tmp_path):
    client, _ = _fake_paid_client(text="付費台詞")
    r, _ = _make_llm_router(monkeypatch, tmp_path, paid_client=client)

    out = await r._call_llm("系統", "x", tier="simple", purpose="dj_narration")

    assert out == "免費台詞"
    assert _rows(tmp_path) == []


@pytest.mark.asyncio
async def test_background_purpose_does_not_exclude_mistral(monkeypatch, tmp_path):
    r, _ = _make_llm_router(monkeypatch, tmp_path)

    await r._call_llm("系統", "x", tier="simple", purpose="summarize_window")

    assert r._llm_bus.ctxs[0].exclude_providers == ()


@pytest.mark.asyncio
async def test_spoken_purpose_excludes_mistral(monkeypatch, tmp_path):
    r, _ = _make_llm_router(monkeypatch, tmp_path)

    await r._call_llm("系統", "x", tier="simple", purpose="generate_player_farewell")

    assert r._llm_bus.ctxs[0].exclude_providers == ("mistral",)
