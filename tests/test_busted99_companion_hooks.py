"""Lane F2：Busted99Cog 在狀態轉換時呼叫 companion bridge 的 emit_game_phase_changed。"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs.busted99_cog import Busted99Cog
from game.busted99.session import Busted99Session, Busted99State, Player99State


def _make_bot_with_bridge(bridge):
    bot = MagicMock()
    bot.companion_bridge = bridge
    bot.cogs.get.return_value = None
    bot.voice_clients = []
    return bot


def _make_session() -> Busted99Session:
    s = Busted99Session(
        session_id=str(uuid.uuid4()),
        guild_id=1,
        channel_id=1,
    )
    s.players.append(Player99State(user_id="u1", display_name="Alice"))
    s.players.append(Player99State(user_id="u2", display_name="Bob"))
    s.players.append(Player99State(user_id="marvin", display_name="Marvin"))
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
    cog = Busted99Cog(bot)
    cog._post_game_message = AsyncMock(return_value=None)
    cog._edit_game_message = AsyncMock(return_value=None)
    cog._play_sfx = AsyncMock(return_value=None)
    cog._spawn = MagicMock(return_value=None)
    cog._channel = None
    return cog, fake_bridge


@pytest.mark.asyncio
async def test_phase_transition_emits_setter_picking(cog_with_bridge):
    """SETTER_PICKING → phase='setter_picking'，current_player 為 setter。"""
    cog, bridge = cog_with_bridge
    session = _make_session()
    session.setter_id = "u2"
    session.state = Busted99State.SETTER_PICKING
    session.round_num = 1

    await cog.on_state_change(session)

    bridge.emit_game_phase_changed.assert_awaited()
    args, kwargs = bridge.emit_game_phase_changed.call_args
    game_name = kwargs.get("game_name") if "game_name" in kwargs else args[0]
    phase = kwargs.get("phase") if "phase" in kwargs else args[1]
    payload = kwargs.get("payload") if "payload" in kwargs else args[2]
    assert game_name == "busted99"
    assert phase == "setter_picking"
    assert payload.get("current_player") == "Bob"
    assert isinstance(payload.get("scoreboard"), list)


@pytest.mark.asyncio
async def test_phase_transition_emits_guessing(cog_with_bridge):
    """GUESSING → phase='guessing'，current_player 為 guesser。"""
    cog, bridge = cog_with_bridge
    session = _make_session()
    session.setter_id = "marvin"
    session.current_guesser_id = "u1"
    session.low_bound = 10
    session.high_bound = 90
    session.state = Busted99State.GUESSING
    session.round_num = 1

    await cog.on_state_change(session)

    bridge.emit_game_phase_changed.assert_awaited()
    args, kwargs = bridge.emit_game_phase_changed.call_args
    phase = kwargs.get("phase") if "phase" in kwargs else args[1]
    payload = kwargs.get("payload") if "payload" in kwargs else args[2]
    assert phase == "guessing"
    assert payload.get("current_player") == "Alice"


@pytest.mark.asyncio
async def test_phase_transition_emits_game_over(cog_with_bridge):
    """GAME_OVER → phase='ended'。"""
    cog, bridge = cog_with_bridge
    session = _make_session()
    session.players[0].score = 200
    session.state = Busted99State.GAME_OVER

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
    cog = Busted99Cog(bot)
    cog._post_game_message = AsyncMock(return_value=None)
    cog._edit_game_message = AsyncMock(return_value=None)
    cog._play_sfx = AsyncMock(return_value=None)
    cog._spawn = MagicMock(return_value=None)

    session = _make_session()
    session.state = Busted99State.JOINING

    await cog.on_state_change(session)

    bridge.emit_game_phase_changed.assert_not_awaited()


@pytest.mark.asyncio
async def test_force_skip_round_method_no_engine():
    """Busted99Cog.force_skip_round 無 active engine 時不爆。"""
    bot = MagicMock()
    bot.companion_bridge = None
    bot.cogs.get.return_value = None
    bot.voice_clients = []
    cog = Busted99Cog(bot)
    await cog.force_skip_round()


@pytest.mark.asyncio
async def test_end_session_method_no_engine():
    """Busted99Cog.end_session 沒有 active engine 時不爆。"""
    bot = MagicMock()
    bot.companion_bridge = None
    bot.cogs.get.return_value = None
    bot.voice_clients = []
    cog = Busted99Cog(bot)
    await cog.end_session()
