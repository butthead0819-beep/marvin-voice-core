"""TDD — E4: 沒音樂時的主動追問只收 life=True 並帶 SpeakKind.MEMORY_CALLBACK。

1. 沒有 life=True 的 callback item 不出價（回 dense 0.0, reason="no_callbacks"）。
2. handler 呼叫 ctrl.speak 時必須帶 kind=SpeakKind.MEMORY_CALLBACK。
"""
import time
from unittest.mock import AsyncMock, MagicMock
import pytest

from intent_agents.memory_callback_agent import MemoryCallbackAgent
from speak_bus import SpeakContext
from suki_memory import MemoryManager
from tts_speak_policy import SpeakKind


def _mk_mem(tmp_path):
    return MemoryManager(
        db_path=str(tmp_path / "mc.db"),
        json_compat_path=str(tmp_path / "mc.json"),
    )


def _mk_ctrl(mem, history=None):
    ctrl = MagicMock()
    ctrl.bot.router.memory = mem
    ctrl.bot.engine.conv_buffer.history = history or []
    ctrl.bot.tts_engine.get_estimated_duration.return_value = 2.0
    ctrl.stream_mode = False
    ctrl.speak = AsyncMock()
    ctrl.last_music_control_time = 0.0
    return ctrl


def _mk_ctx(present_speakers):
    return SpeakContext(
        channel_id=1,
        guild_id=1,
        silence_seconds=0.0,
        present_speakers=present_speakers,
        room_mood=None,
        recent_utterances=[],
        trigger="idle_tick",
    )


@pytest.mark.asyncio
async def test_bid_ignores_non_life_callbacks(monkeypatch, tmp_path):
    """未標記 life=True 的 item（包含舊資料或遊戲操作）不出價。"""
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    mem = _mk_mem(tmp_path)
    # 存一筆非生活操作（life=False）與一筆未帶 life（預設 False）
    mem.enqueue_callback("Alice", "打副本練等", shareable=True, life=False)
    mem.enqueue_callback("Alice", "採集草藥", shareable=True)

    history = [{"speaker": "Alice", "text": "剛剛副本練等怎樣", "timestamp": time.time()}]
    ctrl = _mk_ctrl(mem, history=history)
    agent = MemoryCallbackAgent(ctrl, confidence=0.7, overlap_threshold=0.3)

    bid = await agent.speak_bid(_mk_ctx(["Alice"]))
    assert bid.confidence == 0.0
    assert bid.reason == "no_callbacks"


@pytest.mark.asyncio
async def test_handler_speak_passes_speak_kind_memory_callback(monkeypatch, tmp_path):
    """handler 呼叫 speak 時必須指定 kind=SpeakKind.MEMORY_CALLBACK。"""
    monkeypatch.setenv("SPEAK_MEMORY_CALLBACK", "true")
    mem = _mk_mem(tmp_path)
    mem.enqueue_callback("Alice", "看醫生拿藥", shareable=True, life=True)

    history = [{"speaker": "Alice", "text": "醫生拿藥那件事", "timestamp": time.time()}]
    ctrl = _mk_ctrl(mem, history=history)
    agent = MemoryCallbackAgent(ctrl, confidence=0.7, overlap_threshold=0.3)

    bid = await agent.speak_bid(_mk_ctx(["Alice"]))
    assert bid.confidence == 0.7
    await bid.handler()

    assert ctrl.speak.await_count == 1
    kwargs = ctrl.speak.call_args.kwargs
    assert kwargs.get("kind") == SpeakKind.MEMORY_CALLBACK
