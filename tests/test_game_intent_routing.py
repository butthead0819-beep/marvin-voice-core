"""TDD — 遊戲答案路由遷移到 IntentBus。

背景：三個 game agent（busted/busted99/turtle_soup）格式正確且有單元測試，但
從沒註冊進 IntentBus，遊戲走的是 voice_controller 的硬編碼 cog loop（雙重死碼）。
本檔鎖住遷移後的接線契約：

  1. build_game_ctx → mode="game"（讓 base mode gate 只放行 game agent）
  2. build_intent_agents → 三個 game agent 有註冊，且收到 bot（非 controller）
     —— 傳錯對象會讓 game agent 永遠 cog_not_loaded
  3. 用真正的 agent 清單 + bus：game mode 下 active cog 的答案路由到對的 agent；
     無遊戲 active → 沒人贏 → None（caller 會無條件 return，不 fallback Marvin）
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from cogs.voice_controller import build_game_ctx, build_intent_agents
from intent_bus import IntentBus


# ── 1. build_game_ctx ────────────────────────────────────────────────────────

def test_build_game_ctx_sets_game_mode():
    ctx = build_game_ctx("player1", "我猜 21 點", is_owner=False)
    assert ctx.mode == "game"
    assert ctx.game_mode is True
    assert ctx.raw_text == "我猜 21 點"
    assert ctx.query == "我猜 21 點"
    assert ctx.original_raw == "我猜 21 點"
    assert ctx.wake_intent is None
    assert ctx.speaker == "player1"


# ── 2. build_intent_agents 註冊 + 對象正確 ────────────────────────────────────

def _fake_controller():
    return MagicMock(name="controller")


def _fake_bot_with_cogs(cog_map=None):
    bot = MagicMock(name="bot")
    cog_map = cog_map or {}
    bot.cogs.get = MagicMock(side_effect=lambda name: cog_map.get(name))
    return bot


def test_build_intent_agents_registers_three_game_agents():
    agents = build_intent_agents(_fake_controller(), _fake_bot_with_cogs())
    names = {getattr(a, "name", None) for a in agents}
    assert {"busted", "busted99", "turtle_soup"} <= names


def test_build_intent_agents_game_agents_receive_bot_not_controller():
    """game agent 用 self.bot.cogs 查 cog；必須收到 bot，否則永遠 cog_not_loaded。"""
    controller = _fake_controller()
    bot = _fake_bot_with_cogs()
    agents = build_intent_agents(controller, bot)
    game_agents = [a for a in agents if getattr(a, "name", None) in ("busted", "busted99", "turtle_soup")]
    assert len(game_agents) == 3
    for a in game_agents:
        assert a.bot is bot, f"{a.name} 應收到 bot 而非 controller"


# ── 3. 端到端路由：真正的 agent 清單 + bus ───────────────────────────────────

def _fake_busted99_cog(*, guessing=True, suppress=False):
    cog = MagicMock()
    session = MagicMock()
    state = MagicMock()
    state.name = "GUESSING" if guessing else "IDLE"
    session.state = state
    cog._session = session
    cog.should_suppress_for_game = MagicMock(return_value=suppress)
    cog.receive_voice_answer_by_speaker = AsyncMock(return_value=True)
    return cog


def _bus_for(cog_map):
    """用 build_intent_agents 組真正的 agent 清單 + 一個會 active 的 bot。"""
    bot = _fake_bot_with_cogs(cog_map)
    agents = build_intent_agents(_fake_controller(), bot)
    return IntentBus(agents), bot


@pytest.mark.asyncio
async def test_game_answer_routes_to_active_busted99():
    cog = _fake_busted99_cog(guessing=True, suppress=False)
    bus, _ = _bus_for({"Busted99Cog": cog})
    ctx = build_game_ctx("player1", "猜 50", is_owner=False)

    winner = await bus.dispatch(ctx)

    assert winner is not None
    assert winner.name == "busted99"
    cog.receive_voice_answer_by_speaker.assert_awaited_once_with("player1", "猜 50")


@pytest.mark.asyncio
async def test_no_game_active_no_winner():
    """沒有任何 game cog active → 沒人贏 → None（caller 會 drop，不 fallback Marvin）。"""
    bus, _ = _bus_for({})  # 無 cog
    ctx = build_game_ctx("player1", "隨便講講", is_owner=False)

    winner = await bus.dispatch(ctx)

    assert winner is None


@pytest.mark.asyncio
async def test_suppressed_speaker_no_winner():
    """非猜題者（suppress）→ busted99 agent dense 0.0 → None（drop）。"""
    cog = _fake_busted99_cog(guessing=True, suppress=True)
    bus, _ = _bus_for({"Busted99Cog": cog})
    ctx = build_game_ctx("bystander", "亂入", is_owner=False)

    winner = await bus.dispatch(ctx)

    assert winner is None
    cog.receive_voice_answer_by_speaker.assert_not_awaited()
