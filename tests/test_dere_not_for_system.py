"""9/30：情緒骰子（dere_persona）會把 system prompt 整段換掉。DJ 串場等系統生成（speaker="系統"）
也被擲到，DJ 任務說明消失、模型改回應 user prompt「動態台詞生成」，播出
「隨便說句『世界終將毀滅』，反正沒人會在意」（prod 103 段 LLM 口白中 3 段）。骰子只給真實玩家。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gemini_router_llm import dere_eligible


def test_dere_eligible_rules():
    assert dere_eligible("狗與露", "你是馬文") is True
    assert dere_eligible("系統", "你是 DJ Marvin") is False
    assert dere_eligible(None, "x") is False
    assert dere_eligible("", "x") is False
    assert dere_eligible("狗與露", "...dere_persona...") is False


def _router():
    from gemini_router import GeminiRouter
    r = GeminiRouter.__new__(GeminiRouter)
    r.dna = {"helpfulness": 3}
    r.memory = None
    r.prompt_manager = MagicMock()
    r.prompt_manager.get_instruction = MagicMock(return_value="DERE_PERSONA_PROMPT")
    r._llm_bus = MagicMock()
    r._llm_bus.dispatch = AsyncMock(return_value="ok")
    r._llm_bus.last_dispatch = None
    return r


@pytest.mark.asyncio
@pytest.mark.parametrize("speaker,expect_dere", [("系統", False), ("狗與露", True)])
async def test_call_llm_dice_only_for_players(monkeypatch, speaker, expect_dere):
    import random
    monkeypatch.setenv("LLM_BUS", "true")
    monkeypatch.setattr(random, "random", lambda: 0.0)  # 骰子必中
    r = _router()
    await r._call_llm("你是 DJ Marvin，任務：串場", "動態台詞生成", speaker=speaker, tier="simple")
    sent = r._llm_bus.dispatch.call_args.args[0].system_prompt
    assert (sent == "DERE_PERSONA_PROMPT") is expect_dere
