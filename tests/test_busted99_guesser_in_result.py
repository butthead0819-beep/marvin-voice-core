"""TDD — Busted99Engine: wrong_low/wrong_high result dict 必須包含 guesser_id + guesser_name

Problem:
  _advance_guesser() 在 wrong_low/wrong_high 時把 current_guesser_id 換成下一個人。
  如果 result dict 沒有 guesser_id/guesser_name，_build_guess_result_embed 會拿
  session.current_guesser_id（已經是下一個人）來顯示，造成 embed 寫錯名字。

Tests:
  A) wrong_low → result["guesser_id"] == 猜的人 id
  B) wrong_low → result["guesser_name"] == 猜的人顯示名
  C) wrong_high → result["guesser_id"] / result["guesser_name"] 同上
  D) bust（猜中）→ 也應有 guesser_id / guesser_name（不推進，但一致性）
  E) wrong_low 後 session.current_guesser_id 已換，result dict 內容不受影響
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from game.busted99.session import Busted99Session, Busted99State, Player99State
from game.busted99.engine import Busted99Engine


def _make_two_player_session(answer: int = 50) -> Busted99Session:
    s = Busted99Session.__new__(Busted99Session)
    s.session_id = "test"
    s.guild_id = 1
    s.channel_id = 1
    s.players = [
        Player99State(user_id="u1", display_name="狗與露", score=0),
        Player99State(user_id="u2", display_name="Showay", score=0),
    ]
    s.state = Busted99State.GUESSING
    s.setter_id = "marvin"   # setter 不是 u1/u2
    s.answer = answer
    s.low_bound = 1
    s.high_bound = 99
    s.current_guesser_id = "u1"
    s.guesser_order = ["u1", "u2"]
    s.guessing_queue = ["u2"]  # u2 排在後面
    s.round_num = 1
    s.game_message_id = None
    s.started_at = 0.0
    s.last_guess = None
    s.last_guess_result = None
    s.guess_log = []
    return s


def _make_engine(session: Busted99Session) -> Busted99Engine:
    return Busted99Engine(
        session=session,
        on_state_change=AsyncMock(),
        db_path=":memory:",
    )


# ─── A + E: wrong_low → guesser_id 是猜的人，不是 advance 後的下一個 ────────────

@pytest.mark.asyncio
async def test_wrong_low_result_has_guesser_id():
    session = _make_two_player_session(answer=50)
    engine = _make_engine(session)
    result = await engine.submit_guess("u1", 30)  # 30 < 50 → wrong_low
    assert result["result"] == "wrong_low"
    assert result.get("guesser_id") == "u1", (
        "wrong_low result 必須包含 guesser_id='u1'，"
        "此時 session.current_guesser_id 已 advance 到 u2"
    )


@pytest.mark.asyncio
async def test_wrong_low_result_has_guesser_name():
    session = _make_two_player_session(answer=50)
    engine = _make_engine(session)
    result = await engine.submit_guess("u1", 30)
    assert result.get("guesser_name") == "狗與露", (
        "wrong_low result 必須包含 guesser_name='狗與露'"
    )


@pytest.mark.asyncio
async def test_wrong_low_guesser_id_not_next_guesser():
    """Regression: result["guesser_id"] 不能等於 advance 後的 current_guesser_id。"""
    session = _make_two_player_session(answer=50)
    engine = _make_engine(session)
    result = await engine.submit_guess("u1", 30)
    # After advance, current_guesser_id == "u2"
    assert session.current_guesser_id == "u2", "advance 後 current 應為 u2"
    assert result.get("guesser_id") != "u2", (
        "result['guesser_id'] 不應等於 advance 後的 u2"
    )


# ─── B: wrong_high → guesser_id / guesser_name ───────────────────────────────

@pytest.mark.asyncio
async def test_wrong_high_result_has_guesser_id():
    session = _make_two_player_session(answer=50)
    engine = _make_engine(session)
    result = await engine.submit_guess("u1", 70)  # 70 > 50 → wrong_high
    assert result["result"] == "wrong_high"
    assert result.get("guesser_id") == "u1"


@pytest.mark.asyncio
async def test_wrong_high_result_has_guesser_name():
    session = _make_two_player_session(answer=50)
    engine = _make_engine(session)
    result = await engine.submit_guess("u1", 70)
    assert result.get("guesser_name") == "狗與露"


# ─── C: bust → guesser_id / guesser_name（猜中時 state=GAME_OVER，不 advance）──

@pytest.mark.asyncio
async def test_bust_result_has_guesser_id():
    session = _make_two_player_session(answer=50)
    engine = _make_engine(session)
    result = await engine.submit_guess("u1", 50)  # bust
    assert result["result"] == "bust"
    assert result.get("guesser_id") == "u1"


@pytest.mark.asyncio
async def test_bust_result_has_guesser_name():
    session = _make_two_player_session(answer=50)
    engine = _make_engine(session)
    result = await engine.submit_guess("u1", 50)
    assert result.get("guesser_name") == "狗與露"
