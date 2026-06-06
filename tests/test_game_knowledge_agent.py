"""Tests for GameKnowledgeAgent — 把「馬文幫我查麥塊的鑽石去哪挖」這類遊戲知識查詢
從 intent_gap 模板 ack 升級成真正的 LLM 回答 agent（2026-06-06 Plan 4 ready_to_implement）。

驗證 5 類（對齊 CLAUDE.md IntentBus 測試骨架）：
  - mode gate：game mode → dense 0.0 mode_mismatch（此 agent 只在 normal/stream 出價）
  - trigger miss：非查詢句 → no_match
  - trigger miss：查但無遊戲 marker（如「查歌詞」）→ no_match（避免吃掉音樂/一般查詢）
  - happy path：3 個真實 gap 樣本 → bid 0.80 + handler
  - handler integration：winning handler 呼叫 ctrl._handle_game_knowledge_query(speaker, query)
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from intent_agents.game_knowledge_agent import GameKnowledgeAgent
from intent_bus import IntentContext


def _ctx(raw, speaker="player1", mode="normal", wake_intent=0.9):
    return IntentContext(
        speaker=speaker, raw_text=raw, query=raw, original_raw=raw,
        wake_intent=wake_intent, stream_active=False,
        game_mode=(mode == "game"),
        is_owner=False, now=0.0, mode=mode,
    )


def _agent():
    ctrl = MagicMock()
    ctrl._handle_game_knowledge_query = AsyncMock()
    return GameKnowledgeAgent(ctrl), ctrl


# 3 個真實 intent_gap 樣本（records/agent_gaps.jsonl, game_knowledge_query）
REAL_SAMPLES = [
    "馬文幫我查麥塊的鑽石去哪裡挖",
    "麻煩幫我查麥塊裡的鑽石要去哪裡找",
    "馬文幫我查麥塊遊戲裡的鐵巨人怎麼製作",
]


# ── Mode gating ───────────────────────────────────────────────────────────────

def test_game_mode_dense_zero_mismatch():
    agent, _ = _agent()
    bid = agent.bid(_ctx(REAL_SAMPLES[0], mode="game"))
    assert bid.confidence == 0.0
    assert bid.reason == "mode_mismatch:game"


def test_normal_and_stream_modes_allowed():
    agent, _ = _agent()
    for mode in ("normal", "stream"):
        bid = agent.bid(_ctx(REAL_SAMPLES[0], mode=mode))
        assert bid.confidence == 0.80, f"{mode} 應出價"


# ── Trigger miss (dense 0.0 no_match) ─────────────────────────────────────────

def test_non_query_dense_zero():
    agent, _ = _agent()
    bid = agent.bid(_ctx("馬文今天天氣怎麼樣"))
    assert bid.confidence == 0.0
    assert bid.reason == "no_match"


def test_query_without_game_marker_dense_zero():
    """「查歌詞」「查資料」不該被當遊戲知識查詢（避免吃掉音樂/一般查詢）。"""
    agent, _ = _agent()
    for raw in ("馬文幫我查歌詞", "馬文查一下這首歌", "馬文幫我查資料"):
        bid = agent.bid(_ctx(raw))
        assert bid.confidence == 0.0, f"{raw!r} 不該觸發"
        assert bid.reason == "no_match"


# ── Happy path ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", REAL_SAMPLES)
def test_real_samples_bid(raw):
    agent, _ = _agent()
    bid = agent.bid(_ctx(raw))
    assert bid.confidence == 0.80
    assert bid.handler is not None
    assert "game" in bid.reason or "麥塊" in bid.reason


# ── Handler integration ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handler_calls_controller():
    agent, ctrl = _agent()
    bid = agent.bid(_ctx(REAL_SAMPLES[0], speaker="狗與露"))
    await bid.handler()
    ctrl._handle_game_knowledge_query.assert_awaited_once_with("狗與露", REAL_SAMPLES[0])
