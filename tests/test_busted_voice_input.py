"""TDD — BustedCog 語音輸入銜接

功能：
  1. should_suppress_for_game_by_id(user_id: int)
     BUZZ_LOCKED 時，非搶答者的語音在進 STT 前直接過濾。
  2. receive_voice_answer_by_speaker(speaker: str, text: str)
     由 display_name 反查 user_id，再呼叫 engine.receive_voice_answer()。
  3. game_mode_cap = 0.8
     遊戲啟動後 vc.conv_buffer.game_mode_cap 必須設定，避免短答案被 VAD 截斷。

Tests:
  A1) BUZZ_LOCKED + user_id == buzz_holder → False（不過濾）
  A2) BUZZ_LOCKED + user_id != buzz_holder → True（過濾）
  A3) CLUE_ACTIVE → False（搶答視窗，任何人都能說話）
  A4) IDLE/JOINING/SETTER_INPUT 等狀態 → False
  A5) session 為 None → False（防禦性）
  A6) buzz_holder_id 為 None → False（防禦性）

  B1) speaker 在 _name_to_id → 呼叫 engine.receive_voice_answer(uid, text)
  B2) speaker 不在 _name_to_id → return False
  B3) engine 為 None → return False
  B4) speaker 在 _name_to_id，engine.receive_voice_answer 回傳結果被 relay

  C1) game start → vc.conv_buffer.game_mode_cap == 0.8
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from cogs.game_cog import BustedCog
from game.session import GameSession, GameState, PlayerState


# ─── helpers ─────────────────────────────────────────────────────────────────

def _make_session(state: GameState, buzz_holder_id: str | None = None) -> GameSession:
    s = GameSession.__new__(GameSession)
    s.session_id = "t"
    s.guild_id = 1
    s.channel_id = 1
    s.players = [
        PlayerState(user_id="111", display_name="狗與露"),
        PlayerState(user_id="222", display_name="Showay"),
    ]
    s.state = state
    s.current_setter_id = "333"
    s.current_answer = None
    s.current_clues = []
    s.buzz_holder_id = buzz_holder_id
    s.buzz_locked_until = 0.0
    s.round_num = 1
    s.game_message_id = None
    s.started_at = 0.0
    s.wrong_guesses = []
    s.candidate_themes = []
    s.current_theme = None
    s.setter_hint = None
    s.applied_hint = False
    s.remaining_setters = []
    return s


def _make_cog(session: GameSession | None = None) -> BustedCog:
    bot = MagicMock()
    bot.cogs.get.return_value = None
    bot.voice_clients = []
    cog = BustedCog(bot)
    cog._session = session
    return cog


# ─── A: should_suppress_for_game_by_id ───────────────────────────────────────

def test_suppress_by_id_false_for_buzz_holder():
    """A1: BUZZ_LOCKED + user_id 就是 buzz_holder → 不過濾"""
    s = _make_session(GameState.BUZZ_LOCKED, buzz_holder_id="111")
    cog = _make_cog(s)
    assert cog.should_suppress_for_game_by_id(111) is False


def test_suppress_by_id_true_for_non_holder():
    """A2: BUZZ_LOCKED + 非搶答者 → 過濾"""
    s = _make_session(GameState.BUZZ_LOCKED, buzz_holder_id="111")
    cog = _make_cog(s)
    assert cog.should_suppress_for_game_by_id(222) is True


def test_suppress_by_id_false_during_clue_active():
    """A3: CLUE_ACTIVE → 搶答視窗，任何人都能發聲"""
    s = _make_session(GameState.CLUE_ACTIVE)
    cog = _make_cog(s)
    assert cog.should_suppress_for_game_by_id(222) is False


@pytest.mark.parametrize("state", [
    GameState.IDLE,
    GameState.JOINING,
    GameState.SPINNING,
    GameState.THEME_SELECT,
    GameState.SETTER_INPUT,
    GameState.ROUND_RESULT,
    GameState.GAME_OVER,
])
def test_suppress_by_id_false_outside_buzz_locked(state):
    """A4: 非 BUZZ_LOCKED 狀態一律不過濾"""
    s = _make_session(state, buzz_holder_id="111")
    cog = _make_cog(s)
    assert cog.should_suppress_for_game_by_id(222) is False, f"{state} 不應過濾"


def test_suppress_by_id_false_when_no_session():
    """A5: session 為 None → False"""
    cog = _make_cog(session=None)
    assert cog.should_suppress_for_game_by_id(111) is False


def test_suppress_by_id_false_when_no_buzz_holder():
    """A6: buzz_holder_id 為 None → False"""
    s = _make_session(GameState.BUZZ_LOCKED, buzz_holder_id=None)
    cog = _make_cog(s)
    assert cog.should_suppress_for_game_by_id(111) is False


# ─── B: receive_voice_answer_by_speaker ──────────────────────────────────────

@pytest.mark.asyncio
async def test_voice_answer_by_speaker_found():
    """B1: speaker 在 _name_to_id → 轉交給 engine"""
    cog = _make_cog()
    cog._name_to_id = {"狗與露": 111}
    cog._engine = MagicMock()
    cog._engine.receive_voice_answer = AsyncMock(return_value={"correct": True})
    result = await cog.receive_voice_answer_by_speaker("狗與露", "巨石強森")
    cog._engine.receive_voice_answer.assert_called_once_with(111, "巨石強森")
    assert result == {"correct": True}


@pytest.mark.asyncio
async def test_voice_answer_by_speaker_not_found():
    """B2: speaker 不在 _name_to_id → False"""
    cog = _make_cog()
    cog._name_to_id = {}
    cog._engine = MagicMock()
    cog._engine.receive_voice_answer = AsyncMock()
    result = await cog.receive_voice_answer_by_speaker("陌生人", "任意答案")
    cog._engine.receive_voice_answer.assert_not_called()
    assert result is False


@pytest.mark.asyncio
async def test_voice_answer_by_speaker_engine_none():
    """B3: engine 為 None → False"""
    cog = _make_cog()
    cog._name_to_id = {"狗與露": 111}
    cog._engine = None
    result = await cog.receive_voice_answer_by_speaker("狗與露", "任意答案")
    assert result is False


@pytest.mark.asyncio
async def test_voice_answer_by_speaker_relays_false():
    """B4: engine 回傳 False（狀態不對）時 relay False"""
    cog = _make_cog()
    cog._name_to_id = {"狗與露": 111}
    cog._engine = MagicMock()
    cog._engine.receive_voice_answer = AsyncMock(return_value=False)
    result = await cog.receive_voice_answer_by_speaker("狗與露", "隨便猜")
    assert result is False


# ─── C: game_mode_cap ────────────────────────────────────────────────────────

def test_game_start_sets_game_mode_cap():
    """C1: 遊戲啟動後 vc.conv_buffer.game_mode_cap 設為 0.8"""
    bot = MagicMock()
    bot.voice_clients = []

    vc = MagicMock()
    vc.conv_buffer = MagicMock()
    vc.conv_buffer.game_mode_cap = None
    vc.stream_mode = False
    vc.radio_mode = False

    def mock_cogs_get(name):
        if name == "VoiceController":
            return vc
        return None

    bot.cogs.get.side_effect = mock_cogs_get
    cog = BustedCog(bot)
    cog._set_game_mode(True)
    assert vc.conv_buffer.game_mode_cap == 0.8, (
        "遊戲啟動後 conv_buffer.game_mode_cap 必須設為 0.8"
    )


def test_game_stop_clears_game_mode_cap():
    """C2: 遊戲結束後 vc.conv_buffer.game_mode_cap 恢復 None"""
    bot = MagicMock()
    bot.voice_clients = []

    vc = MagicMock()
    vc.conv_buffer = MagicMock()
    vc.conv_buffer.game_mode_cap = 0.8
    vc.stream_mode = False
    vc.radio_mode = False

    def mock_cogs_get(name):
        if name == "VoiceController":
            return vc
        return None

    bot.cogs.get.side_effect = mock_cogs_get
    cog = BustedCog(bot)
    cog._set_game_mode(False)
    assert vc.conv_buffer.game_mode_cap is None
