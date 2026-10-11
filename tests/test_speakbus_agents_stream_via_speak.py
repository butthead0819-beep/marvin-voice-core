"""
SpeakBus agent stream-awareness 統一化（Option B）：

1. MemoryCallbackAgent（事件記憶 Phase 2）handler 走
   `vc.speak(text, proactive=True, allow_dual=False, kind=SpeakKind.MEMORY_CALLBACK)`。

2. BridgeAgent._speak_bridge 走
   `vc.speak(text, proactive=True)` 而非 `play_tts(...)`，自動接 hotswap：
   - 非 stream → 正常播
   - stream + ≤30 字 → hotswap 注入
   - stream + 超字 → silent fallback
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from event_memory import EventStore
from intent_agents.memory_callback_agent import MemoryCallbackAgent
from speak_bus import SpeakContext
from tts_speak_policy import SpeakKind


# ── helpers ───────────────────────────────────────────────────────────────


def _mk_event(owner="Alice", activity="露營", event_date=None, heard_by=None, event_id=1):
    now = time.time()
    return {
        "id": event_id, "guild_id": 0, "owner": owner, "activity": activity,
        "place": None, "event_date": event_date if event_date is not None else now - 2 * 86400,
        "date_text": "", "participants": [], "heard_by": heard_by or [owner],
        "quotes": [], "first_seen": now, "last_seen": now, "created_at": now,
    }


def _mk_ctrl():
    """Minimal controller stub。voice mode gating 已升到 SpeakBus 層，agent 不再讀。"""
    ctrl = MagicMock()
    ctrl.bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)
    ctrl.speak = AsyncMock()
    ctrl.play_tts = AsyncMock()
    ctrl.stt_logger = MagicMock()
    return ctrl


def _mk_ctx(present_speakers, last_speaker=None, mode="normal"):
    return SpeakContext(
        channel_id=1, guild_id=1, silence_seconds=0.0,
        present_speakers=present_speakers, room_mood=None,
        recent_utterances=[], trigger="idle_tick", mode=mode,
        last_speaker=last_speaker, last_text=None,
    )


# ── 1. MemoryCallback 在 normal mode 仍正常 bid ─────────────────────────────

@pytest.mark.asyncio
async def test_memory_callback_bid_still_works_when_not_in_stream(monkeypatch):
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    store = EventStore(":memory:")
    agent = MemoryCallbackAgent(_mk_ctrl(), store=store, confidence=0.7)
    agent._candidates = [_mk_event()]

    bid = await agent.speak_bid(_mk_ctx(["Alice"], last_speaker="Alice"))

    assert bid.confidence == 0.7
    assert bid.agent_name == "MemoryCallbackAgent"


# ── 2. MemoryCallback handler 走 vc.speak ────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_callback_speak_uses_speak_helper(monkeypatch):
    """handler 呼叫 ctrl.speak(text, proactive=True, allow_dual=False, kind=MEMORY_CALLBACK)。"""
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    store = EventStore(":memory:")
    ctrl = _mk_ctrl()
    agent = MemoryCallbackAgent(ctrl, store=store, confidence=0.7)
    agent._candidates = [_mk_event()]

    bid = await agent.speak_bid(_mk_ctx(["Alice"], last_speaker="Alice"))
    await bid.handler()

    ctrl.speak.assert_awaited_once()
    ctrl.play_tts.assert_not_called()
    _, kwargs = ctrl.speak.call_args
    assert kwargs.get("proactive") is True
    assert kwargs.get("allow_dual") is False
    assert kwargs.get("kind") == SpeakKind.MEMORY_CALLBACK


# ── 3. BridgeAgent handler 走 vc.speak ──────────────────────────────────────


@pytest.mark.asyncio
async def test_bridge_agent_speak_uses_speak_helper():
    """BridgeAgent._speak_bridge 改呼叫 ctrl.speak(text, proactive=True)。"""
    from bridge_agent import BridgeAgent

    ctrl = MagicMock()
    ctrl.speak = AsyncMock()
    ctrl.play_tts = AsyncMock()
    graph = MagicMock()
    graph.mark_bridged = MagicMock()

    agent = BridgeAgent.__new__(BridgeAgent)
    agent._ctrl = ctrl
    agent._graph = graph
    agent.name = "BridgeAgent"

    await agent._speak_bridge(
        "Alice", {"speaker": "Bob", "transcript_id": 99, "text": "之前說的事"}
    )

    ctrl.speak.assert_awaited_once()
    ctrl.play_tts.assert_not_called()
    _, kwargs = ctrl.speak.call_args
    assert kwargs.get("proactive") is True
    graph.mark_bridged.assert_called_once_with(99)
