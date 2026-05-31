"""DualSpeakAgent 註冊到 build_intent_agents 的 regression test。

驗證：
  - DualSpeakAgent 出現在 agent list 內
  - 該 agent 在 bot.router 上綁好 llm_fn（透過 make_gemini_dual_dialogue_llm_fn）

目的：未來重構 agent list 時若有人不小心刪掉這條，會立刻紅燈。
"""
from __future__ import annotations

from unittest.mock import MagicMock

from cogs.voice_controller import build_intent_agents
from intent_agents.dual_speak_agent import DualSpeakAgent


def test_dual_speak_agent_registered():
    controller = MagicMock()
    bot = MagicMock()
    # router 必須 truthy；不必真實 — make_gemini_dual_dialogue_llm_fn 只拿來綁
    bot.router = MagicMock()

    agents = build_intent_agents(controller, bot)
    dual_agents = [a for a in agents if isinstance(a, DualSpeakAgent)]
    assert len(dual_agents) == 1, "DualSpeakAgent 應該被註冊一次（且只一次）"

    # llm_fn 應已綁定（不是 None）
    assert dual_agents[0].llm_fn is not None
