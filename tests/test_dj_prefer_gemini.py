"""TDD — generate_dynamic_system_msg：dj_interjection 呼叫 _call_llm 時帶
prefer_providers=DJ_PREFER_PROVIDERS（優先 gemini_free → groq），其他 event_type
不受影響（維持原本呼叫簽名，避免這些呼叫站的既有測試 fake 因多一個 kwarg 炸掉）。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gemini_router_content import DJ_PREFER_PROVIDERS, GeminiRouterContentMixin


def _make_mixin():
    inst = GeminiRouterContentMixin.__new__(GeminiRouterContentMixin)
    inst.dna = {}
    inst._dyn_msg_cache = False  # 繞過快取，確保一定會呼叫 _call_llm
    inst._call_llm = AsyncMock(return_value="唉...")
    return inst


@pytest.mark.asyncio
async def test_dj_interjection_passes_prefer_providers():
    mixin = _make_mixin()
    await mixin.generate_dynamic_system_msg("dj_interjection", context="周杰倫 七里香")

    _, kwargs = mixin._call_llm.call_args
    assert kwargs.get("prefer_providers") == DJ_PREFER_PROVIDERS


@pytest.mark.asyncio
async def test_non_dj_event_does_not_pass_prefer_providers():
    mixin = _make_mixin()
    # cooldown 帶 context 避開 is_quip 批次路徑，走單句 _call_llm 呼叫。
    await mixin.generate_dynamic_system_msg("cooldown", context="玩家一直煩你")

    _, kwargs = mixin._call_llm.call_args
    assert "prefer_providers" not in kwargs


def _bus_router():
    from gemini_router import GeminiRouter
    r = GeminiRouter.__new__(GeminiRouter)
    r.dna = {"helpfulness": 3}
    r.memory = None
    r.prompt_manager = MagicMock()
    r._llm_bus = MagicMock()
    r._llm_bus.dispatch = AsyncMock(return_value="ok")
    r._llm_bus.last_dispatch = None
    return r


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs,expected", [
    ({"prefer_providers": ("gemini_free", "groq")}, ("gemini_free", "groq")),
    ({}, ()),
])
async def test_call_llm_forwards_prefer_providers_to_bus(monkeypatch, kwargs, expected):
    monkeypatch.setenv("LLM_BUS", "true")
    r = _bus_router()
    await r._call_llm("你是 DJ Marvin，任務：串場", "動態台詞生成", speaker="系統", tier="simple", **kwargs)
    assert r._llm_bus.dispatch.call_args.args[0].prefer_providers == expected
