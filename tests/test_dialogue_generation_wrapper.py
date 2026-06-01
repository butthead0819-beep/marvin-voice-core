"""make_gemini_dual_dialogue_llm_fn — router 綁定 wrapper。

驗證：
  - wrapper 回 callable 符合 llm_fn 簽名 (system, user) -> str
  - 內部呼叫 router._call_cloud(system, user, is_json=True)
    （直連 Gemini 跳過 LLM Bus，避開 bus 過期模型 404 問題）
  - router._call_cloud 例外往上拋（caller 的 generate_dual_dialogue 已 catch）
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.dialogue_generation import make_gemini_dual_dialogue_llm_fn


@pytest.mark.asyncio
async def test_wrapper_passes_prompts_to_router_call_cloud():
    router = MagicMock()
    router._call_cloud = AsyncMock(return_value='{"segments":[]}')

    llm_fn = make_gemini_dual_dialogue_llm_fn(router)
    result = await llm_fn("SYS prompt", "USR prompt")

    assert result == '{"segments":[]}'
    router._call_cloud.assert_awaited_once()
    args, kwargs = router._call_cloud.call_args
    assert args[0] == "SYS prompt"
    assert args[1] == "USR prompt"
    assert kwargs["is_json"] is True


@pytest.mark.asyncio
async def test_wrapper_propagates_router_exception():
    router = MagicMock()
    router._call_cloud = AsyncMock(side_effect=RuntimeError("router boom"))

    llm_fn = make_gemini_dual_dialogue_llm_fn(router)
    with pytest.raises(RuntimeError, match="router boom"):
        await llm_fn("s", "u")


@pytest.mark.asyncio
async def test_wrapper_does_not_call_call_llm_or_bus():
    """Regression: wrapper 必須走 _call_cloud 直連、不能繞回 _call_llm（會踩 LLM Bus 模型 404）。"""
    router = MagicMock()
    router._call_cloud = AsyncMock(return_value="ok")
    router._call_llm = AsyncMock(return_value="should not be called")

    llm_fn = make_gemini_dual_dialogue_llm_fn(router)
    await llm_fn("s", "u")

    router._call_cloud.assert_awaited_once()
    router._call_llm.assert_not_called()
