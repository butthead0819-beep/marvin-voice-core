"""DJ 口白 system prompt 要帶節奏規則（docs/tts_breath_plan.md Phase 3）。

edge-tts 只靠標點換氣，長句會一口氣念完；DJ 口白送進 LLM 的 system prompt
要帶 SPOKEN_RHYTHM_RULE，非 DJ（純評語/cooldown）不帶。
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from gemini_router_content import SPOKEN_RHYTHM_RULE, GeminiRouterContentMixin


def _make_mixin():
    inst = GeminiRouterContentMixin.__new__(GeminiRouterContentMixin)
    inst.dna = {}
    inst._dyn_msg_cache = False  # 繞過快取，確保一定會走生成路徑
    inst._call_llm = AsyncMock(return_value="唉...")
    return inst


@pytest.mark.asyncio
async def test_dj_interjection_system_prompt_includes_rhythm_rule():
    mixin = _make_mixin()

    await mixin.generate_dynamic_system_msg("dj_interjection", context="周杰倫 七里香")

    args, _ = mixin._call_llm.call_args
    sys_prompt = args[0]
    assert SPOKEN_RHYTHM_RULE in sys_prompt


@pytest.mark.asyncio
async def test_cooldown_system_prompt_excludes_rhythm_rule():
    mixin = _make_mixin()

    await mixin.generate_dynamic_system_msg("cooldown", context="玩家一直煩你")

    args, _ = mixin._call_llm.call_args
    sys_prompt = args[0]
    assert SPOKEN_RHYTHM_RULE not in sys_prompt
