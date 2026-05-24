"""TDD — Plan C4: GeminiRouterLLMMixin._call_llm wrapper pattern.

env LLM_BUS=true → 走 LLMBus.dispatch
env LLM_BUS=false / unset → 走 legacy try/except chain
**禁雙跑** — 同一 call 不會兩條都跑（會 TPM 雙計，Risk 3）。

驗證:
1. flag unset → legacy 路徑被呼叫，bus 沒被呼叫
2. flag true → bus 路徑被呼叫，legacy 沒被呼叫
3. tier → min_quality 對應正確 (simple/medium/high)
4. system_prompt / json_mode / temperature 從 _call_llm 參數正確 forward 到 LLMContext
5. bus NoLLMAvailable → 回 empty string（caller 兜底既有 empty handling）
6. speaker 傳到 bus 觸發 stickiness（指標 sanity）

Phase 1 範圍：只 wrap `_call_llm`（非 streaming），streaming methods 留 legacy 不動。
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — minimal GeminiRouterLLMMixin instance for testing
# ---------------------------------------------------------------------------

def _make_router_mixin():
    """Build a minimal GeminiRouterLLMMixin instance with legacy LLM clients mocked."""
    from gemini_router_llm import GeminiRouterLLMMixin

    obj = GeminiRouterLLMMixin.__new__(GeminiRouterLLMMixin)
    # 必填 attribute 給 _call_llm 用
    obj.dna = {"helpfulness": 3}
    obj.prompt_manager = MagicMock()
    obj.vision_enabled = False
    obj.memory = MagicMock()
    obj.is_exhausted = False
    obj.budget = MagicMock()
    obj.budget.is_circuit_open.return_value = False
    obj.budget.add_tokens = MagicMock()
    obj.groq_dedicated_client = MagicMock()
    obj.groq_simple_model = "llama-3.1-8b-instant"
    obj.groq_fallback_model = "llama-3.3-70b-versatile"
    obj.cerebras_client = None  # 不跑 cerebras 分支
    obj.cerebras_model = None
    obj.current_tier = "Tier-1"
    obj.on_fallback_callback = None
    obj.model_name = "gemini-test"
    obj._llm_bus = None  # 預設沒 inject
    obj._dispatch_fallback_chain = AsyncMock(return_value="fallback_response")
    return obj


def _mock_legacy_chat_resp(content="legacy_response", total_tokens=100):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    resp.usage = MagicMock(total_tokens=total_tokens)
    return resp


# ---------------------------------------------------------------------------
# 1. Flag unset / false → legacy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flag_unset_uses_legacy_path(monkeypatch):
    monkeypatch.delenv("LLM_BUS", raising=False)
    obj = _make_router_mixin()
    obj.groq_dedicated_client.chat.completions.create = AsyncMock(
        return_value=_mock_legacy_chat_resp("legacy"))
    obj._llm_bus = MagicMock()
    obj._llm_bus.dispatch = AsyncMock(return_value="should_not_be_called")

    result = await obj._call_llm("sys", "user", tier="simple")
    assert result == "legacy"
    obj._llm_bus.dispatch.assert_not_awaited(), "bus 不該被呼叫"
    obj.groq_dedicated_client.chat.completions.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_flag_false_uses_legacy_path(monkeypatch):
    monkeypatch.setenv("LLM_BUS", "false")
    obj = _make_router_mixin()
    obj.groq_dedicated_client.chat.completions.create = AsyncMock(
        return_value=_mock_legacy_chat_resp("legacy"))
    obj._llm_bus = MagicMock()
    obj._llm_bus.dispatch = AsyncMock(return_value="should_not_be_called")

    result = await obj._call_llm("sys", "user")
    assert result == "legacy"
    obj._llm_bus.dispatch.assert_not_awaited()


# ---------------------------------------------------------------------------
# 2. Flag true → bus path（禁雙跑）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flag_true_uses_bus_path(monkeypatch):
    monkeypatch.setenv("LLM_BUS", "true")
    obj = _make_router_mixin()
    obj.groq_dedicated_client.chat.completions.create = AsyncMock(
        return_value=_mock_legacy_chat_resp("should_not_be_called"))
    obj._llm_bus = MagicMock()
    obj._llm_bus.dispatch = AsyncMock(return_value="bus_response")

    result = await obj._call_llm("sys", "user", tier="medium")
    assert result == "bus_response"
    obj._llm_bus.dispatch.assert_awaited_once()
    obj.groq_dedicated_client.chat.completions.create.assert_not_awaited(), \
        "禁雙跑 — flag true 時 legacy chain 不該被觸碰"


# ---------------------------------------------------------------------------
# 3. tier → min_quality mapping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("tier,expected_quality", [
    ("simple", "fast"),
    ("medium", "balanced"),
    ("high", "high"),
])
async def test_tier_maps_to_min_quality(monkeypatch, tier, expected_quality):
    monkeypatch.setenv("LLM_BUS", "true")
    obj = _make_router_mixin()
    obj._llm_bus = MagicMock()
    obj._llm_bus.dispatch = AsyncMock(return_value="ok")

    await obj._call_llm("sys", "user", tier=tier)
    ctx = obj._llm_bus.dispatch.call_args.args[0]
    assert ctx.min_quality == expected_quality, \
        f"tier={tier} 該對應 min_quality={expected_quality}，實際 {ctx.min_quality}"


# ---------------------------------------------------------------------------
# 4. system_prompt / json_mode / temperature / speaker propagate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_call_llm_forwards_system_prompt_to_bus(monkeypatch):
    monkeypatch.setenv("LLM_BUS", "true")
    obj = _make_router_mixin()
    obj._llm_bus = MagicMock()
    obj._llm_bus.dispatch = AsyncMock(return_value="ok")

    await obj._call_llm("[system content]", "[user content]")
    ctx = obj._llm_bus.dispatch.call_args.args[0]
    assert ctx.system_prompt == "[system content]"
    assert ctx.prompt == "[user content]"


@pytest.mark.asyncio
async def test_call_llm_forwards_json_mode_to_bus(monkeypatch):
    monkeypatch.setenv("LLM_BUS", "true")
    obj = _make_router_mixin()
    obj._llm_bus = MagicMock()
    obj._llm_bus.dispatch = AsyncMock(return_value="ok")

    await obj._call_llm("sys", "user", is_json=True)
    ctx = obj._llm_bus.dispatch.call_args.args[0]
    assert ctx.json_mode is True


@pytest.mark.asyncio
async def test_call_llm_forwards_temperature_to_bus(monkeypatch):
    monkeypatch.setenv("LLM_BUS", "true")
    obj = _make_router_mixin()
    obj._llm_bus = MagicMock()
    obj._llm_bus.dispatch = AsyncMock(return_value="ok")

    await obj._call_llm("sys", "user", temperature=0.42)
    ctx = obj._llm_bus.dispatch.call_args.args[0]
    assert ctx.temperature == 0.42


@pytest.mark.asyncio
async def test_call_llm_forwards_speaker_to_bus(monkeypatch):
    """F4 stickiness 要 speaker 在 ctx — 沒 forward = stickiness 無效."""
    monkeypatch.setenv("LLM_BUS", "true")
    obj = _make_router_mixin()
    obj._llm_bus = MagicMock()
    obj._llm_bus.dispatch = AsyncMock(return_value="ok")

    await obj._call_llm("sys", "user", speaker="alice")
    ctx = obj._llm_bus.dispatch.call_args.args[0]
    assert ctx.speaker == "alice"


# ---------------------------------------------------------------------------
# 5. Bus NoLLMAvailable → empty string (caller 兜底)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bus_no_available_returns_empty_string_not_fallback_legacy(monkeypatch):
    """Risk 3: 禁從 bus 失敗 fallback 到 legacy（會雙計）。bus NoLLMAvailable → 回 ''."""
    from llm_agents.base import NoLLMAvailable
    monkeypatch.setenv("LLM_BUS", "true")
    obj = _make_router_mixin()
    obj.groq_dedicated_client.chat.completions.create = AsyncMock(
        return_value=_mock_legacy_chat_resp("should_not_fallback"))
    obj._llm_bus = MagicMock()
    obj._llm_bus.dispatch = AsyncMock(side_effect=NoLLMAvailable("test"))

    result = await obj._call_llm("sys", "user")
    assert result == "", f"bus 失敗該回 ''，實際: {result!r}"
    obj.groq_dedicated_client.chat.completions.create.assert_not_awaited(), \
        "禁雙跑 — bus 失敗不該 fallback legacy"


# ---------------------------------------------------------------------------
# 6. Bus 未注入時 → flag on 視同 unset，走 legacy（safe degradation）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flag_true_but_bus_not_injected_falls_to_legacy(monkeypatch):
    """_llm_bus is None（bot 啟動時 bus 沒裝起來）→ flag 視同 off，跑 legacy。
    保護單元：避免 bus init 出錯 + flag on 直接整個 LLM pipeline 死。"""
    monkeypatch.setenv("LLM_BUS", "true")
    obj = _make_router_mixin()
    obj._llm_bus = None  # 沒裝
    obj.groq_dedicated_client.chat.completions.create = AsyncMock(
        return_value=_mock_legacy_chat_resp("legacy"))

    result = await obj._call_llm("sys", "user")
    assert result == "legacy"
