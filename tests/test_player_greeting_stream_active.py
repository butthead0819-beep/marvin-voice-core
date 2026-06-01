"""
打招呼 / 送客的 stream_active 訊號注入：

stream 模式中要走 hotswap 注入（STREAM_BUDGET=30 字），LLM 產出必須 ≤30 字
才能通過 is_hotswap_eligible 閘。Prompt 原本寫 20 字（>30 沒問題），但 LLM 偶爾
超字。stream_active=True 時額外在 user_prompt 注入「請務必≤30字」的硬提醒，
讓 LLM 更傾向產短句。

非 stream（stream_active=False）→ 維持原本「20 字內」baseline，不加額外提醒。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_mixin():
    """獨立實例化 GeminiRouterContentMixin（不啟整個 GeminiRouter）。"""
    from gemini_router_content import GeminiRouterContentMixin

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


# ── 1. stream_active=True → user_prompt 含「30 字」硬提醒 ───────────────────

@pytest.mark.asyncio
async def test_greeting_stream_active_injects_short_directive():
    mixin = _make_mixin()
    await mixin.generate_player_greeting("狗與露", stream_active=True)

    args, _ = mixin._call_llm.call_args
    user_prompt = args[1]
    assert "30" in user_prompt, f"stream_active=True 時應在 user_prompt 注入 30 字提醒，實際: {user_prompt!r}"
    assert "字" in user_prompt


@pytest.mark.asyncio
async def test_farewell_stream_active_injects_short_directive():
    mixin = _make_mixin()
    await mixin.generate_player_farewell("狗與露", stream_active=True)

    args, _ = mixin._call_llm.call_args
    user_prompt = args[1]
    assert "30" in user_prompt
    assert "字" in user_prompt


# ── 2. stream_active=False (預設) → 不注入額外提醒 ──────────────────────────

@pytest.mark.asyncio
async def test_greeting_default_no_stream_directive():
    mixin = _make_mixin()
    await mixin.generate_player_greeting("狗與露")

    args, _ = mixin._call_llm.call_args
    user_prompt = args[1]
    assert "30" not in user_prompt, "非 stream 時不該注入 30 字提醒"


@pytest.mark.asyncio
async def test_farewell_default_no_stream_directive():
    mixin = _make_mixin()
    await mixin.generate_player_farewell("狗與露")

    args, _ = mixin._call_llm.call_args
    user_prompt = args[1]
    assert "30" not in user_prompt


# ── 3. 不破壞原本回傳與快取 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_greeting_still_caches():
    mixin = _make_mixin()
    msg1 = await mixin.generate_player_greeting("狗與露", stream_active=True)
    msg2 = await mixin.generate_player_greeting("狗與露", stream_active=True)
    assert msg1 == msg2
    assert mixin._call_llm.await_count == 1, "重複呼叫應走快取，只觸發 1 次 LLM"
