"""TDD — T5: 聽>>講 policy gate — intimate mode suppresses unprompted output.

三條防線：
  A. speak_bus_tick_loop  — ON 跳過 _speak_bus.tick；OFF 正常 tick。
  B. trigger_proactive_topic — ON 跳過 get_proactive_topics；OFF 正常進入。
  C. slow_system_loop diary — ON 跳過 maybe_render_diary；OFF 正常渲染。

TDD 流程：先紅後綠——guard 未加時 ON-suppression case FAIL（tick 被呼叫到）。
"""
from __future__ import annotations

import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── A: speak_bus_tick_loop ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_speak_bus_tick_loop_intimate_on_suppresses_tick():
    """_intimate_mode=True → SpeakBus.tick 永遠不被呼叫。"""
    from cogs.voice_controller import VoiceController

    fake = MagicMock()
    fake._intimate_mode = True
    fake._speak_bus.tick = AsyncMock()

    await VoiceController.speak_bus_tick_loop.coro(fake)

    fake._speak_bus.tick.assert_not_called()


@pytest.mark.asyncio
async def test_speak_bus_tick_loop_intimate_off_passes_through():
    """_intimate_mode=False → guard 不短路，_speak_bus.tick 被 await。"""
    from cogs.voice_controller import VoiceController

    bid = MagicMock()
    bid.handler = AsyncMock()
    bid.confidence = 0.9
    bid.reason = "test"
    bid.agent_name = "test_agent"

    ctx = MagicMock()
    ctx.trigger = "idle_tick"
    ctx.silence_seconds = 10.0
    ctx.present_speakers = []

    fake = MagicMock()
    fake._intimate_mode = False
    fake.bot.voice_clients = [MagicMock()]
    fake._speak_bus.agents.return_value = [MagicMock()]
    fake._speak_bus.tick = AsyncMock(return_value=bid)
    fake._build_speak_context.return_value = ctx
    fake._mood_agent.observe = AsyncMock()
    fake._record_speak_outcome_after = AsyncMock()

    await VoiceController.speak_bus_tick_loop.coro(fake)

    fake._speak_bus.tick.assert_awaited_once()


# ── B: trigger_proactive_topic ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trigger_proactive_topic_intimate_on_suppresses():
    """_intimate_mode=True → get_proactive_topics 不被呼叫（主動起話題路徑關閉）。"""
    from cogs.voice_controller import VoiceController

    fake = MagicMock()
    fake._intimate_mode = True
    fake.bot.router.memory.get_proactive_topics = MagicMock()

    await VoiceController.trigger_proactive_topic(fake)

    fake.bot.router.memory.get_proactive_topics.assert_not_called()


@pytest.mark.asyncio
async def test_trigger_proactive_topic_intimate_off_reaches_topics():
    """_intimate_mode=False → guard 不短路，進入 get_proactive_topics。"""
    from cogs.voice_controller import VoiceController

    fake = MagicMock()
    fake._intimate_mode = False
    fake.get_online_members.return_value = ["user1"]
    # return [] → 早退（沒話題），但 get_proactive_topics 已被呼叫
    fake.bot.router.memory.get_proactive_topics = MagicMock(return_value=[])

    await VoiceController.trigger_proactive_topic(fake)

    fake.bot.router.memory.get_proactive_topics.assert_called_once()


# ── C: slow_system_loop diary ─────────────────────────────────────────────────

def _make_diary_fake(*, intimate_on: bool) -> MagicMock:
    """造一個能走到 diary 分支的 fake self（空累積器, silence=400s>300）。"""
    fake = MagicMock()
    fake._intimate_mode = intimate_on
    fake.bot.engine.conv_buffer = MagicMock()           # truthy → 不提早 return
    fake.bot.engine.conv_buffer.pop_new_entries.return_value = []
    fake.slow_loop_accumulator = []                     # 真 list，extend/truthiness 正確
    fake.last_player_speech_time = time.time() - 400   # silence=400 > 300
    fake.stream_mode = False
    fake.get_online_members.return_value = []           # 跳過 post_open_rituals
    fake.radio_mode = True                              # 跳過 radio / freq-adj elif
    fake.active_text_channel = MagicMock()
    return fake


@pytest.mark.asyncio
async def test_slow_system_loop_intimate_on_skips_diary():
    """_intimate_mode=True → maybe_render_diary 不被呼叫。"""
    from cogs.voice_controller import VoiceController

    fake = _make_diary_fake(intimate_on=True)
    mock_render = AsyncMock()

    with patch("diary_comic_poster.maybe_render_diary", mock_render):
        await VoiceController.slow_system_loop.coro(fake)

    mock_render.assert_not_called()


@pytest.mark.asyncio
async def test_slow_system_loop_intimate_off_calls_diary():
    """_intimate_mode=False → maybe_render_diary 被 await（公開日記正常渲染）。"""
    from cogs.voice_controller import VoiceController

    fake = _make_diary_fake(intimate_on=False)
    mock_render = AsyncMock()

    with patch("diary_comic_poster.maybe_render_diary", mock_render):
        await VoiceController.slow_system_loop.coro(fake)

    mock_render.assert_awaited_once()
