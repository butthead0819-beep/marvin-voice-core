"""make_gemini_dual_dialogue_llm_fn — router 綁定 wrapper。

驗證：
  - wrapper 回 callable 符合 llm_fn 簽名 (system, user) -> str
  - 內部呼叫 router._call_llm 帶 is_json=True / allow_local=False / tier=high
    （走 LLM Bus 由 bid loop 挑當期 provider）
  - router._call_llm 例外往上拋（caller 的 generate_dual_dialogue 已 catch）
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.dialogue_generation import make_gemini_dual_dialogue_llm_fn


@pytest.mark.asyncio
async def test_wrapper_passes_prompts_to_router_call_llm():
    router = MagicMock()
    router._call_llm = AsyncMock(return_value='{"segments":[]}')

    llm_fn = make_gemini_dual_dialogue_llm_fn(router)
    result = await llm_fn("SYS prompt", "USR prompt")

    assert result == '{"segments":[]}'
    router._call_llm.assert_awaited_once()
    args, kwargs = router._call_llm.call_args
    assert args[0] == "SYS prompt"
    assert args[1] == "USR prompt"
    assert kwargs["is_json"] is True
    assert kwargs["allow_local"] is False
    assert kwargs["tier"] == "high"


@pytest.mark.asyncio
async def test_wrapper_propagates_router_exception():
    router = MagicMock()
    router._call_llm = AsyncMock(side_effect=RuntimeError("router boom"))

    llm_fn = make_gemini_dual_dialogue_llm_fn(router)
    with pytest.raises(RuntimeError, match="router boom"):
        await llm_fn("s", "u")
