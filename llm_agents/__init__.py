"""LLM dispatch agent bus — Marvin dispatch points map #2 (memory: project_agent_pattern.md).

把 gemini_router_llm.py 內 try/except chain 換成 select-max-confidence bid bus.
新供應商 = 加一個 agent class，不改 chain.

Phase 1 範圍：base + GroqAgent + feature flag 並行（見 plan
~/.gstack/projects/Discord-voice-bot/jackhuang-main-plan-llm-router-agent-20260524-141726.md）。
"""
from llm_agents.base import (
    KNOWN_PURPOSES,
    LLMAgent,
    LLMBid,
    LLMBus,
    LLMContext,
    NoLLMAvailable,
)

__all__ = [
    "KNOWN_PURPOSES",
    "LLMAgent",
    "LLMBid",
    "LLMBus",
    "LLMContext",
    "NoLLMAvailable",
]
