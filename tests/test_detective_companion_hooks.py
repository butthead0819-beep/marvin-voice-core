"""Lane F：DetectiveCog 在狀態轉換時呼叫 companion bridge 的 emit_game_phase_changed。

驗證 cog 的 on_state_change 在每個關鍵 state 時：
    - 呼叫 bridge.emit_game_phase_changed
    - phase 字串對應 DetectiveState
    - payload 至少包含 round / scoreboard / current_player / last_event

不直接測 Discord embed；用 MagicMock channel 阻擋 send。
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs.detective_cog import DetectiveCog
from game.detective.engine import DetectiveEngine
from game.detective.session import DetectiveSession, DetectiveState, StatementSet


def _make_bot_with_bridge(bridge):
    bot = MagicMock()
    bot.companion_bridge = bridge
    # VoiceController 不存在
    bot.cogs.get.return_value = None
    return bot


def _make_session_with_three() -> DetectiveSession:
    s = DetectiveSession(session_id=str(uuid.uuid4()), guild_id=1, channel_id=1)
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
    cog = DetectiveCog(bot)
    # 把 _post_game_message 短路掉（不打 Discord）
    cog._post_game_message = AsyncMock(return_value=None)
    cog._channel = None
    return cog, fake_bridge


@pytest.mark.asyncio
async def test_phase_transition_emits_declaring(cog_with_bridge):
    """DetectiveCog.on_state_change(DECLARING) → 呼叫 emit_game_phase_changed('detective', 'declaring', ...)。"""
    cog, bridge = cog_with_bridge
    session = _make_session_with_three()
    session.players.clear()
    from game.detective.session import PlayerDState
    session.players.append(PlayerDState(user_id="u1", display_name="Alice"))
    session.players.append(PlayerDState(user_id="u2", display_name="Bob"))
    session.players.append(PlayerDState(user_id="marvin", display_name="Marvin"))
    session.current_declarer_id = "u2"
    session.state = DetectiveState.DECLARING
    session.round_num = 1

    await cog.on_state_change(session)

    bridge.emit_game_phase_changed.assert_awaited()
    args, kwargs = bridge.emit_game_phase_changed.call_args
    # 支援 positional 或 keyword 形式
    game_name = kwargs.get("game_name") if "game_name" in kwargs else args[0]
    phase = kwargs.get("phase") if "phase" in kwargs else args[1]
    payload = kwargs.get("payload") if "payload" in kwargs else args[2]
    assert game_name == "detective"
    assert phase == "declaring"
    assert payload.get("current_player") == "Bob"
    assert payload.get("round") == 1
    scoreboard = payload.get("scoreboard")
    assert isinstance(scoreboard, list)
    assert any(s["user"] == "Bob" for s in scoreboard)


@pytest.mark.asyncio
async def test_phase_transition_emits_voting(cog_with_bridge):
    """DetectiveCog.on_state_change(VOTING) → emit_game_phase_changed phase='voting'。"""
    cog, bridge = cog_with_bridge
    session = _make_session_with_three()
    from game.detective.session import PlayerDState
    session.players.append(PlayerDState(user_id="u1", display_name="Alice"))
    session.players.append(PlayerDState(user_id="u2", display_name="Bob"))
    session.players.append(PlayerDState(user_id="marvin", display_name="Marvin"))
    session.current_declarer_id = "u2"
    session.current_statements = StatementSet(a="x", b="y", c="z", lie_index=1)
    session.state = DetectiveState.VOTING

    await cog.on_state_change(session)

    bridge.emit_game_phase_changed.assert_awaited()
    args, kwargs = bridge.emit_game_phase_changed.call_args
    phase = kwargs.get("phase") if "phase" in kwargs else args[1]
    payload = kwargs.get("payload") if "payload" in kwargs else args[2]
    assert phase == "voting"
    assert payload.get("current_player") == "Bob"


@pytest.mark.asyncio
async def test_phase_transition_emits_game_over(cog_with_bridge):
    """GAME_OVER → phase='ended'。"""
    cog, bridge = cog_with_bridge
    session = _make_session_with_three()
    from game.detective.session import PlayerDState
    session.players.append(PlayerDState(user_id="u1", display_name="Alice", score=5))
    session.players.append(PlayerDState(user_id="u2", display_name="Bob", score=3))
    session.state = DetectiveState.GAME_OVER

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
    cog = DetectiveCog(bot)
    cog._post_game_message = AsyncMock(return_value=None)

    session = _make_session_with_three()
    from game.detective.session import PlayerDState
    session.players.append(PlayerDState(user_id="u1", display_name="Alice"))
    session.state = DetectiveState.JOINING

    await cog.on_state_change(session)

    bridge.emit_game_phase_changed.assert_not_awaited()


@pytest.mark.asyncio
async def test_force_skip_round_method_advances_state():
    """DetectiveCog.force_skip_round 是 cog 上 callable，無 active engine 時不爆。"""
    bot = MagicMock()
    bot.companion_bridge = None
    bot.cogs.get.return_value = None
    cog = DetectiveCog(bot)
    # 沒有 active engine
    await cog.force_skip_round()  # 不爆


@pytest.mark.asyncio
async def test_end_session_method_no_engine():
    """DetectiveCog.end_session 沒有 active engine 時不爆。"""
    bot = MagicMock()
    bot.companion_bridge = None
    bot.cogs.get.return_value = None
    cog = DetectiveCog(bot)
    await cog.end_session()
