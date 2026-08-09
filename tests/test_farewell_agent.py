"""FarewellAgent — 喚醒後直接對 Marvin 說「掰掰/晚安/bye bye」的互道再見 intent。

8/9 使用者提報：喚醒說「Marvin byebye」/「馬文晚安」/「馬文再見」，Marvin 沒有真的
道別、也沒有 TTS——根因是 `_query_quality_gate` 把這幾個短道別詞當雜訊擋掉，query
沒機會走到 bus。本檔測 agent 本身（patterns + handler）；gate 放行測在
test_wake_ux.py。

confidence 0.90，1 個 intent：farewell。mode_compatible = {"normal", "stream"}。
Handler 直接呼叫 ctrl._handle_wake_farewell(speaker)。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_bus import IntentContext


pytestmark = pytest.mark.asyncio


def _ctx(query: str, mode: str = "normal") -> IntentContext:
    return IntentContext(
        speaker="alice",
        raw_text=query,
        query=query,
        original_raw=query,
        wake_intent=0.9,
        stream_active=(mode == "stream"),
        game_mode=(mode == "game"),
        is_owner=False,
        now=0.0,
        mode=mode,
    )


def _ctrl():
    ctrl = MagicMock()
    ctrl._handle_wake_farewell = AsyncMock()
    return ctrl


# ── mode gate ─────────────────────────────────────────────────────────────


async def test_game_mode_returns_mode_mismatch():
    from intent_agents.farewell_agent import FarewellAgent
    agent = FarewellAgent(_ctrl())
    bid = agent.bid(_ctx("掰掰", mode="game"))
    assert bid.confidence == 0.0
    assert "mode_mismatch" in bid.reason


# ── pattern coverage ─────────────────────────────────────────────────────


@pytest.mark.parametrize("query", [
    # ctx.query 在真實流程已被 _strip_wake_word 剝過（見 build_intent_agents 呼叫點），
    # 這裡不含帶喚醒詞的原句
    "晚安", "再見", "掰掰", "拜拜", "掰了", "掰",
    "bye bye", "byebye", "goodbye", "goodnight",
])
async def test_farewell_patterns(query):
    from intent_agents.farewell_agent import FarewellAgent
    agent = FarewellAgent(_ctrl())
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.90, f"expected 0.90 for {query!r}, got {bid.confidence}"


@pytest.mark.parametrize("query", ["stream"], ids=["stream_mode"])
async def test_farewell_matches_in_stream_mode(query):
    from intent_agents.farewell_agent import FarewellAgent
    agent = FarewellAgent(_ctrl())
    bid = agent.bid(_ctx("掰掰", mode=query))
    assert bid.confidence == 0.90


@pytest.mark.parametrize("query", [
    "今天天氣不錯",
    "播放周杰倫",
    "下一首",
    "把音量調小一點",
])
async def test_no_match_returns_dense_zero(query):
    from intent_agents.farewell_agent import FarewellAgent
    agent = FarewellAgent(_ctrl())
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.0


# ── handler integration ───────────────────────────────────────────────────


async def test_handler_calls_wake_farewell():
    from intent_agents.farewell_agent import FarewellAgent
    ctrl = _ctrl()
    agent = FarewellAgent(ctrl)
    bid = agent.bid(_ctx("晚安"))
    await bid.handler()
    ctrl._handle_wake_farewell.assert_called_once_with("alice")


async def test_handler_swallows_handle_exception():
    from intent_agents.farewell_agent import FarewellAgent
    ctrl = _ctrl()
    ctrl._handle_wake_farewell = AsyncMock(side_effect=RuntimeError("boom"))
    agent = FarewellAgent(ctrl)
    bid = agent.bid(_ctx("晚安"))
    await bid.handler()  # 不該 raise


async def test_handler_missing_method_does_not_crash():
    from intent_agents.farewell_agent import FarewellAgent
    ctrl = _ctrl()
    del ctrl._handle_wake_farewell
    agent = FarewellAgent(ctrl)
    bid = agent.bid(_ctx("晚安"))
    await bid.handler()  # 不該 raise


# ── ctrl._handle_wake_farewell 本體：貼文字＋TTS(protected) ─────────────────


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.router = MagicMock()
    bot.router.generate_player_farewell = AsyncMock(return_value="掰掰，路上小心。")
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.post_summon_callback = None

    from unittest.mock import patch
    with patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        cog = VoiceController(bot)
    cog.stt_logger = MagicMock()
    cog.play_tts = AsyncMock()
    cog.stream_mode = False
    return cog


async def test_handle_wake_farewell_speaks_and_posts():
    cog = _make_cog()
    cog.active_text_channel = AsyncMock()

    await cog._handle_wake_farewell("狗與露")

    cog.active_text_channel.send.assert_awaited_once()
    cog.play_tts.assert_awaited_once()
    args, kwargs = cog.play_tts.await_args
    assert "掰掰" in args[0]
    assert kwargs.get("protected") is True
