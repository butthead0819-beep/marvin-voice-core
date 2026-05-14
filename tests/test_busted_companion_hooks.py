"""Lane F2：BustedCog 在狀態轉換時呼叫 companion bridge 的 emit_game_phase_changed。

驗證 cog 的 on_state_change 在每個關鍵 state 時：
    - 呼叫 bridge.emit_game_phase_changed(game_name="busted", phase=..., payload=...)
    - phase 字串對應 GameState
    - payload 至少含 round / scoreboard / current_player / last_event

不直接測 Discord embed；用 MagicMock channel 阻擋 send。
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs.game_cog import BustedCog
from game.session import GameSession, GameState, PlayerState


def _make_bot_with_bridge(bridge):
    bot = MagicMock()
    bot.companion_bridge = bridge
    bot.cogs.get.return_value = None
    bot.voice_clients = []
    return bot


def _make_session() -> GameSession:
    s = GameSession(
        session_id=str(uuid.uuid4()),
        guild_id=1,
        channel_id=1,
    )
    s.players.append(PlayerState(user_id="u1", display_name="Alice"))
    s.players.append(PlayerState(user_id="u2", display_name="Bob"))
    s.players.append(PlayerState(user_id="marvin", display_name="Marvin"))
    return s


@pytest.fixture
def fake_bridge():
    b = MagicMock()
    b.is_running = True
    b.emit_game_phase_changed = AsyncMock()
    return b


@pytest.fixture
def cog_with_bridge(fake_bridge):
    bot = _make_bot_with_bridge(fake_bridge)
    cog = BustedCog(bot)
    # 把 _post_game_message / _edit_game_message / _play_sfx 短路掉
    cog._post_game_message = AsyncMock(return_value=None)
    cog._edit_game_message = AsyncMock(return_value=None)
    cog._play_sfx = AsyncMock(return_value=None)
    # 阻擋背景任務真的跑起來
    cog._spawn = MagicMock(return_value=None)
    cog._channel = None
    return cog, fake_bridge


@pytest.mark.asyncio
async def test_phase_transition_emits_joining(cog_with_bridge):
    """on_state_change(JOINING) → 廣播 phase='joining'。"""
    cog, bridge = cog_with_bridge
    session = _make_session()
    session.state = GameState.JOINING
    session.round_num = 1

    await cog.on_state_change(session)

    bridge.emit_game_phase_changed.assert_awaited()
    args, kwargs = bridge.emit_game_phase_changed.call_args
    game_name = kwargs.get("game_name") if "game_name" in kwargs else args[0]
    phase = kwargs.get("phase") if "phase" in kwargs else args[1]
    payload = kwargs.get("payload") if "payload" in kwargs else args[2]
    assert game_name == "busted"
    assert phase == "joining"
    assert isinstance(payload.get("scoreboard"), list)


@pytest.mark.asyncio
async def test_phase_transition_emits_clue_active(cog_with_bridge):
    """CLUE_ACTIVE → phase='clue_active'，current_player 為 setter。"""
    cog, bridge = cog_with_bridge
    session = _make_session()
    session.current_setter_id = "u2"
    session.current_answer = "黑洞"
    session.current_clues = ["太空中的天體"]
    session.state = GameState.CLUE_ACTIVE
    session.round_num = 2

    await cog.on_state_change(session)

    bridge.emit_game_phase_changed.assert_awaited()
    args, kwargs = bridge.emit_game_phase_changed.call_args
    phase = kwargs.get("phase") if "phase" in kwargs else args[1]
    payload = kwargs.get("payload") if "payload" in kwargs else args[2]
    assert phase == "clue_active"
    assert payload.get("current_player") == "Bob"
    assert payload.get("round") == 2


@pytest.mark.asyncio
async def test_phase_transition_emits_game_over(cog_with_bridge):
    """GAME_OVER → phase='ended'。"""
    cog, bridge = cog_with_bridge
    session = _make_session()
    session.players[0].score = 100
    session.players[1].score = 80
    session.state = GameState.GAME_OVER

    await cog.on_state_change(session)

    bridge.emit_game_phase_changed.assert_awaited()
    args, kwargs = bridge.emit_game_phase_changed.call_args
    phase = kwargs.get("phase") if "phase" in kwargs else args[1]
    assert phase == "ended"


@pytest.mark.asyncio
async def test_emit_safely_skipped_when_bridge_not_running():
    """bridge.is_running=False → 不嘗試呼叫 emit；不爆。"""
    bridge = MagicMock()
    bridge.is_running = False
    bridge.emit_game_phase_changed = AsyncMock()
    bot = _make_bot_with_bridge(bridge)
    cog = BustedCog(bot)
    cog._post_game_message = AsyncMock(return_value=None)
    cog._edit_game_message = AsyncMock(return_value=None)
    cog._play_sfx = AsyncMock(return_value=None)
    cog._spawn = MagicMock(return_value=None)

    session = _make_session()
    session.state = GameState.JOINING

    await cog.on_state_change(session)

    bridge.emit_game_phase_changed.assert_not_awaited()


@pytest.mark.asyncio
async def test_force_skip_round_method_no_engine():
    """BustedCog.force_skip_round 無 active engine 時不爆。"""
    bot = MagicMock()
    bot.companion_bridge = None
    bot.cogs.get.return_value = None
    bot.voice_clients = []
    cog = BustedCog(bot)
    await cog.force_skip_round()  # 不爆


@pytest.mark.asyncio
async def test_end_session_method_no_engine():
    """BustedCog.end_session 沒有 active engine 時不爆。"""
    bot = MagicMock()
    bot.companion_bridge = None
    bot.cogs.get.return_value = None
    bot.voice_clients = []
    cog = BustedCog(bot)
    await cog.end_session()
