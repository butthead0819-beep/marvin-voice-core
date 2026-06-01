"""
SpeakBus agent stream-awareness 統一化（Option B）：

1. MemoryCallbackAgent.speak_bid stream_mode 時回 dense 0.0("stream_mode")
   — 看齊 ProactiveTopicAgent / BridgeAgent，補既有缺口（silent 中標 bug）。

2. MemoryCallbackAgent._speak_callback / BridgeAgent._speak_bridge 走
   `vc.speak(text, proactive=True)` 而非 `play_tts(...)`，自動接 hotswap：
   - 非 stream → 正常播
   - stream + ≤30 字 → hotswap 注入
   - stream + 超字 → silent fallback
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_agents.memory_callback_agent import MemoryCallbackAgent
from speak_bus import SpeakContext
from suki_memory import MemoryManager


# ── helpers (與 test_memory_callback_agent.py 同 pattern) ────────────────────


def _mk_mem(tmp_path):
    return MemoryManager(
        db_path=str(tmp_path / "mc.db"),
        json_compat_path=str(tmp_path / "mc.json"),
    )


def _mk_ctrl(mem, history=None):
    """Minimal controller stub。voice mode gating 已升到 SpeakBus 層，agent 不再讀。"""
    ctrl = MagicMock()
    ctrl.bot.router.memory = mem
    ctrl.bot.engine.conv_buffer.history = history or []
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


def _utt(speaker, text, ts_offset_s=0.0):
    return {"speaker": speaker, "text": text, "timestamp": time.time() + ts_offset_s}


# ── 1. MemoryCallback 在 normal mode 仍正常 bid（agent 不再 ad-hoc gate stream）─

@pytest.mark.asyncio
async def test_memory_callback_bid_still_works_when_not_in_stream(monkeypatch, tmp_path):
    """非 stream 行為不變（原本能贏的 case 仍贏）。

    Note: stream_mode gate 已升級到 SpeakBus 層（mode_compatible），覆蓋於
    test_speakbus_mode_compatible / test_speakbus_agents_mode_compatible。
    """
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    mem = _mk_mem(tmp_path)
    mem.enqueue_callback("Alice", "試 grounded search", shareable=True)
    history = [_utt("Alice", "grounded search 那個怎樣")]
    agent = MemoryCallbackAgent(
        _mk_ctrl(mem, history=history),
        confidence=0.7, overlap_threshold=0.3,
    )

    bid = await agent.speak_bid(_mk_ctx(["Alice"], last_speaker="Alice"))

    assert bid.confidence == 0.7
    assert bid.agent_name == "MemoryCallbackAgent"


# ── 2. MemoryCallback handler 走 vc.speak ────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_callback_speak_uses_speak_helper(monkeypatch, tmp_path):
    """_speak_callback 改呼叫 ctrl.speak(text, proactive=True)，不直接打 play_tts。"""
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    mem = _mk_mem(tmp_path)
    mem.enqueue_callback("Alice", "試 grounded search", shareable=True)
    history = [_utt("Alice", "grounded search 怎樣了")]
    ctrl = _mk_ctrl(mem, history=history)
    agent = MemoryCallbackAgent(ctrl, confidence=0.7, overlap_threshold=0.3)

    bid = await agent.speak_bid(_mk_ctx(["Alice"], last_speaker="Alice"))
    await bid.handler()

    ctrl.speak.assert_awaited_once()
    ctrl.play_tts.assert_not_called()
    _, kwargs = ctrl.speak.call_args
    assert kwargs.get("proactive") is True


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
