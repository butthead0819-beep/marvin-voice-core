"""個人化進場招呼查表：

熟面孔進場走固定的「司儀播報」一句話（押韻、扣個人喜好），
命中查表就直接回傳——不呼叫 LLM、不吃 1 小時快取、不會因記憶漂移亂講話。
沒建檔的路人維持原本的 LLM 生成路徑。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gemini_router_content import PERSONAL_GREETINGS, GeminiRouterContentMixin


def _make_mixin():
    inst = GeminiRouterContentMixin.__new__(GeminiRouterContentMixin)
    inst._greeting_cache = {}
    inst._farewell_cache = {}
    inst.vision_enabled = True
    inst.dna = {}
    inst.memory = MagicMock()
    inst.temp_toxicity_override = None
    inst.prompt_manager = MagicMock()
    inst.prompt_manager.get_instruction = MagicMock(return_value="[fake system prompt]")
    inst._call_llm = AsyncMock(return_value="阿，又是你。")
    return inst


@pytest.mark.asyncio
async def test_known_player_returns_static_phrase_without_llm():
    mixin = _make_mixin()
    for name, phrase in PERSONAL_GREETINGS.items():
        mixin._call_llm.reset_mock()
        msg = await mixin.generate_player_greeting(name)
        assert msg == phrase
        mixin._call_llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_known_player_static_even_in_stream_mode():
    mixin = _make_mixin()
    msg = await mixin.generate_player_greeting("大肚", stream_active=True)
    assert msg == PERSONAL_GREETINGS["大肚"]
    mixin._call_llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_known_player_does_not_touch_cache():
    mixin = _make_mixin()
    await mixin.generate_player_greeting("狗與露")
    assert mixin._greeting_cache == {}


@pytest.mark.asyncio
async def test_unknown_player_falls_through_to_llm():
    mixin = _make_mixin()
    msg = await mixin.generate_player_greeting("路人甲")
    mixin._call_llm.assert_awaited_once()
    assert "路人甲" in msg  # 名字保證前綴仍生效


def test_every_phrase_contains_its_name_and_fits_hotswap_budget():
    for name, phrase in PERSONAL_GREETINGS.items():
        assert name in phrase, f"{name} 的招呼句沒叫到名字：{phrase!r}"
        assert len(phrase) <= 30, f"{name} 的招呼句超過 30 字（stream 插播唸不完）：{phrase!r}"
