"""speechdna 的 LLM 呼叫改走 bus（2026-06-04）。

背景：analyze_speech_dna 原本只建一個 Groq client，topic 分類 + 風格合成全打 Groq
llama-3.3-70b。5/31 週日跑時撞 Groq TPD 100k 上限 → 大量 batch 429 → 大肚 style_summary
留空（無 failover）。改走 llm_pool 的 analyze tier：Groq 爆時自動讓位 Cerebras / Gemini
2.5 free（per-model 獨立配額）等，不再單點失敗。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from llm_pool import CooldownAwarePool, PoolEndpoint, TieredLLMRouter
from scripts.analyze_speech_dna import _llm_call


def _fake_analyze_ep(content: str):
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(total_tokens=42),
    )
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=resp)
    return PoolEndpoint(name="groq-analyze", client=client, model="llama-3.3-70b-versatile")


@pytest.mark.asyncio
async def test_llm_call_routes_through_bus_analyze_tier():
    """_llm_call 拿到 TieredLLMRouter → 走 analyze tier，回該 endpoint 的內容。"""
    ep = _fake_analyze_ep("1. casual\n2. work")
    router = TieredLLMRouter(CooldownAwarePool([]), CooldownAwarePool([ep]))

    out = await _llm_call(router, "請分類", max_tokens=300)

    assert out == "1. casual\n2. work"
    assert router.usage_by_caller["speechdna"] == 42      # 歸屬到 speechdna caller


@pytest.mark.asyncio
async def test_llm_call_bus_all_exhausted_returns_empty_string():
    """bus 全爆（analyze 回 None）→ _llm_call 回 ""（讓上層 graceful 留空、不炸）。"""
    router = TieredLLMRouter(CooldownAwarePool([]), CooldownAwarePool([]))
    out = await _llm_call(router, "x")
    assert out == ""
