"""TDD — Busted99 STT dispatch 層早期過濾（should_suppress_for_game_by_id）

功能：在 full-STT 開始前（_full_stt_inflight 遞增前），依 user_id 判斷是否為非猜題者。
      非猜題者的音訊直接 return，不佔 STT 並發名額。

Tests:
  A) GUESSING 狀態 + user_id 是猜題者 → False（不過濾，讓 STT 繼續）
  B) GUESSING 狀態 + user_id 不是猜題者 → True（過濾）
  C) IDLE/JOINING 狀態 → False（不過濾，正常流程）
  D) session 為 None → False（防禦性）
  E) current_guesser_id 為 None → False（防禦性）
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from game.busted99.session import Busted99Session, Busted99State, Player99State


def _make_cog_with_session(state: Busted99State, guesser_id: str | None = "11111"):
    from cogs.busted99_cog import Busted99Cog
    bot = MagicMock()
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    cog = Busted99Cog(bot)

    session = Busted99Session.__new__(Busted99Session)
    session.session_id = "test"
    session.guild_id = 1
    session.channel_id = 1
    session.players = [
        Player99State(user_id="11111", display_name="狗與露", score=0),
        Player99State(user_id="22222", display_name="Showay", score=0),
    ]
    session.state = state
    session.setter_id = "marvin"
    session.answer = 50
    session.low_bound = 1
    session.high_bound = 99
    session.current_guesser_id = guesser_id
    session.guessing_queue = []
    session.round_num = 1
    session.game_message_id = None
    session.started_at = 0.0
    session.last_guess = None
    session.last_guess_result = None
    session.guess_log = []

    cog._session = session
    return cog, session


# ─── A: GUESSING + 猜題者 → False ────────────────────────────────────────────

def test_suppress_by_id_returns_false_for_current_guesser():
    cog, _ = _make_cog_with_session(Busted99State.GUESSING, guesser_id="11111")
    result = cog.should_suppress_for_game_by_id(11111)
    assert result is False, "猜題者不應被過濾"


# ─── B: GUESSING + 非猜題者 → True ───────────────────────────────────────────

def test_suppress_by_id_returns_true_for_non_guesser():
    cog, _ = _make_cog_with_session(Busted99State.GUESSING, guesser_id="11111")
    result = cog.should_suppress_for_game_by_id(22222)  # Showay，不是猜題者
    assert result is True, "非猜題者應被過濾"


# ─── C: 非 GUESSING 狀態 → False ─────────────────────────────────────────────

@pytest.mark.parametrize("state", [
    Busted99State.IDLE,
    Busted99State.JOINING,
    Busted99State.SETTER_PICKING,
    Busted99State.GAME_OVER,
])
def test_suppress_by_id_returns_false_outside_guessing(state):
    cog, _ = _make_cog_with_session(state, guesser_id="11111")
    # 不論哪個 user，非 GUESSING 狀態下一律不過濾
    result = cog.should_suppress_for_game_by_id(22222)
    assert result is False, f"{state} 狀態下不應過濾"


# ─── D: session 為 None → False ──────────────────────────────────────────────

def test_suppress_by_id_returns_false_when_no_session():
    from cogs.busted99_cog import Busted99Cog
    bot = MagicMock()
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    cog = Busted99Cog(bot)
    cog._session = None
    result = cog.should_suppress_for_game_by_id(11111)
    assert result is False


# ─── E: current_guesser_id 為 None → False ───────────────────────────────────

def test_suppress_by_id_returns_false_when_no_guesser_id():
    cog, _ = _make_cog_with_session(Busted99State.GUESSING, guesser_id=None)
    result = cog.should_suppress_for_game_by_id(22222)
    assert result is False, "guesser_id 為 None 時不應過濾（防禦性）"
