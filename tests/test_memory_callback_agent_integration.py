"""MemoryCallbackAgent × SpeakBus integration test (plan-eng-review T6)。

跑通真實 SpeakBus.tick → 多 agent bid → multiplier → winner → handler 路徑。
不 mock SpeakBus 本身，只 mock VoiceController 必要 surface。
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_agents.memory_callback_agent import MemoryCallbackAgent
from proactive_topic_agent import ProactiveTopicAgent
from speak_bus import SpeakBus, SpeakContext
from suki_memory import MemoryManager


def _utt(speaker, text, ts_offset_s=0.0):
    return {"speaker": speaker, "text": text, "timestamp": time.time() + ts_offset_s}


def _mk_mem(tmp_path):
    return MemoryManager(
        db_path=str(tmp_path / "i.db"),
        json_compat_path=str(tmp_path / "i.json"),
    )


def _mk_ctrl_for_both(mem, history):
    """同時滿足 ProactiveTopicAgent + MemoryCallbackAgent 的 stub。"""
    ctrl = MagicMock()
    # MemoryCallbackAgent 用
    ctrl.bot.router.memory = mem
    ctrl.bot.engine.conv_buffer.history = history
    ctrl.bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)
    ctrl.speak = AsyncMock(return_value=None)  # 2026-06-01: handler 改走 vc.speak()
    ctrl.play_tts = AsyncMock(return_value=None)
    ctrl.stt_logger = MagicMock()
    # ProactiveTopicAgent 用（最小 surface）
    ctrl.proactive_silence_threshold = 300.0
    ctrl.last_proactive_time = 0.0
    ctrl.radio_mode = False
    ctrl.stream_mode = False
    ctrl.active_text_channel = SimpleNamespace(id=100)
    ctrl.bot.router.current_game = None
    # ProactiveTopicAgent.speak_bid 跑 trigger_proactive_topic（handler 才用，bid 階段不執行）
    return ctrl


def _ctx(present, last_speaker="Alice", silence_seconds=400.0):
    return SpeakContext(
        channel_id=100,
        guild_id=1,
        silence_seconds=silence_seconds,
        present_speakers=list(present),
        room_mood=None,
        recent_utterances=[],
        trigger="idle_tick",
        last_speaker=last_speaker,
    )


# ── integration tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tick_returns_memory_callback_bid_and_handler_consumes(monkeypatch, tmp_path):
    """SpeakBus.tick 拿到 MemoryCallback bid → await handler → consume_callback 跑掉。"""
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    mem = _mk_mem(tmp_path)
    mem.enqueue_callback("Alice", "試 grounded search", shareable=True)
    history = [_utt("Alice", "grounded search 那個怎樣")]
    ctrl = _mk_ctrl_for_both(mem, history)

    bus = SpeakBus()
    bus.register(MemoryCallbackAgent(ctrl, confidence=0.7, overlap_threshold=0.3))

    bid = await bus.tick(_ctx(["Alice"]))
    assert bid is not None
    assert bid.agent_name == "MemoryCallbackAgent"
    assert bid.confidence == pytest.approx(0.7)

    await bid.handler()
    assert ctrl.speak.await_count == 1
    assert mem.peek_all_shareable_callbacks("Alice") == []  # consumed


@pytest.mark.asyncio
async def test_memory_callback_beats_proactive_topic_when_both_eligible(monkeypatch, tmp_path):
    """兩個 agent 都能 bid → MemoryCallback (0.7) 贏 ProactiveTopic (0.6)。"""
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    mem = _mk_mem(tmp_path)
    mem.enqueue_callback("Alice", "試 grounded search", shareable=True)
    history = [_utt("Alice", "grounded search 那個")]
    ctrl = _mk_ctrl_for_both(mem, history)

    bus = SpeakBus()
    bus.register(ProactiveTopicAgent(ctrl, confidence=0.6))  # 預設 0.6
    bus.register(MemoryCallbackAgent(ctrl, confidence=0.7, overlap_threshold=0.3))

    bid = await bus.tick(_ctx(["Alice"]))
    assert bid is not None
    assert bid.agent_name == "MemoryCallbackAgent"
    assert bid.confidence == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_ducking_multiplier_preserves_memory_callback_winner(monkeypatch, tmp_path):
    """multiplier=0.5：MemoryCallback effective 0.35 仍過 MIN_CONFIDENCE 0.30，
    且 0.35 > ProactiveTopic 0.30 → MemoryCallback 仍贏。"""
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    mem = _mk_mem(tmp_path)
    mem.enqueue_callback("Alice", "試 grounded search", shareable=True)
    history = [_utt("Alice", "grounded search 那個")]
    ctrl = _mk_ctrl_for_both(mem, history)

    bus = SpeakBus()
    bus.register(ProactiveTopicAgent(ctrl, confidence=0.6))
    bus.register(MemoryCallbackAgent(ctrl, confidence=0.7, overlap_threshold=0.3))
    bus.set_global_multiplier(0.5, ttl_s=60.0)

    bid = await bus.tick(_ctx(["Alice"]))
    assert bid is not None
    assert bid.agent_name == "MemoryCallbackAgent"
    assert bid.confidence == pytest.approx(0.35)  # effective after multiplier
