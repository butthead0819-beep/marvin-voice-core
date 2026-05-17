"""TDD — Busted99 guess_log

功能：每次 submit_guess 後，session.guess_log 累積一筆紀錄，
      ws_state 也包含 guess_log，讓 Web UI 顯示猜題歷史。
      timeout_guesser() 也寫入一筆 result="timeout"。

Tests:
  A) 初始 session.guess_log 為空 list
  B) wrong_low → guess_log 有一筆，含 guesser / guess / result / low / high
  C) wrong_high → guess_log 有一筆，含正確欄位
  D) bust → guess_log 最後一筆 result == "bust"
  E) 多次猜題 → guess_log 依序累積（不蓋掉）
  F) _build_ws_state 包含 guess_log 欄位
  G) guess_log 項目有 round_num 欄位
  H) timeout_guesser() → guess_log 有一筆 result="timeout"，guess=None
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from game.busted99.session import Busted99Session, Busted99State, Player99State
from game.busted99.engine import Busted99Engine


def _make_session(answer: int = 50) -> Busted99Session:
    s = Busted99Session.__new__(Busted99Session)
    s.session_id = "test"
    s.guild_id = 1
    s.channel_id = 1
    s.players = [
        Player99State(user_id="u1", display_name="狗與露", score=0),
        Player99State(user_id="u2", display_name="Showay", score=0),
    ]
    s.state = Busted99State.GUESSING
    s.setter_id = "marvin"
    s.answer = answer
    s.low_bound = 1
    s.high_bound = 99
    s.current_guesser_id = "u1"
    s.guesser_order = ["u1", "u2"]
    s.guessing_queue = ["u2"]
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


# ─── A: 初始 guess_log 為空 ───────────────────────────────────────────────────

def test_session_guess_log_initial_empty():
    from game.busted99.session import Busted99Session
    s = Busted99Session(session_id="x", guild_id=1, channel_id=1)
    assert hasattr(s, "guess_log"), "Busted99Session 必須有 guess_log 屬性"
    assert s.guess_log == [], "初始 guess_log 必須為空 list"


# ─── B: wrong_low → guess_log 有一筆 ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_wrong_low_appends_to_guess_log():
    session = _make_session(answer=50)
    engine = _make_engine(session)
    await engine.submit_guess("u1", 30)  # wrong_low
    assert len(session.guess_log) == 1
    entry = session.guess_log[0]
    assert entry["guesser"] == "狗與露"
    assert entry["guess"] == 30
    assert entry["result"] == "wrong_low"
    assert "low" in entry
    assert "high" in entry


# ─── C: wrong_high → guess_log 有一筆 ────────────────────────────────────────

@pytest.mark.asyncio
async def test_wrong_high_appends_to_guess_log():
    session = _make_session(answer=50)
    engine = _make_engine(session)
    await engine.submit_guess("u1", 70)  # wrong_high
    assert len(session.guess_log) == 1
    entry = session.guess_log[0]
    assert entry["result"] == "wrong_high"
    assert entry["guess"] == 70
    assert entry["guesser"] == "狗與露"


# ─── D: bust → guess_log 最後一筆 result == "bust" ───────────────────────────

@pytest.mark.asyncio
async def test_bust_appends_to_guess_log():
    session = _make_session(answer=50)
    engine = _make_engine(session)
    await engine.submit_guess("u1", 50)  # bust
    assert len(session.guess_log) == 1
    assert session.guess_log[0]["result"] == "bust"


# ─── E: 多次猜題 → 依序累積 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_multiple_guesses_accumulate_in_log():
    session = _make_session(answer=50)
    engine = _make_engine(session)
    await engine.submit_guess("u1", 30)  # wrong_low, current → u2
    await engine.submit_guess("u2", 70)  # wrong_high, current → u1 (round 2)
    assert len(session.guess_log) == 2
    assert session.guess_log[0]["result"] == "wrong_low"
    assert session.guess_log[1]["result"] == "wrong_high"
    # 順序保持（不是 prepend）
    assert session.guess_log[0]["guess"] == 30
    assert session.guess_log[1]["guess"] == 70


# ─── F: _build_ws_state 包含 guess_log ───────────────────────────────────────

@pytest.mark.asyncio
async def test_build_ws_state_includes_guess_log():
    from cogs.busted99_cog import Busted99Cog
    from unittest.mock import MagicMock
    bot = MagicMock()
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    cog = Busted99Cog(bot)

    session = _make_session(answer=50)
    # 手動塞一筆假 log
    session.guess_log = [{"guesser": "狗與露", "guess": 30, "result": "wrong_low", "low": 30, "high": 99, "round": 1}]
    cog._session = session

    state = cog._build_ws_state(session)
    assert "guess_log" in state, "_build_ws_state 必須包含 guess_log 欄位"
    assert len(state["guess_log"]) == 1
    assert state["guess_log"][0]["guesser"] == "狗與露"


# ─── G: guess_log 項目有 round_num ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_guess_log_entry_has_round_num():
    session = _make_session(answer=50)
    session.round_num = 3
    engine = _make_engine(session)
    await engine.submit_guess("u1", 30)
    assert "round" in session.guess_log[0]
    assert session.guess_log[0]["round"] == 3


# ─── H: timeout_guesser → guess_log result="timeout" ────────────────────────

@pytest.mark.asyncio
async def test_timeout_guesser_appends_to_guess_log():
    """
    timeout_guesser() 觸發後，guess_log 應有一筆 result="timeout"、
    guesser 是超時者的名稱、guess 為 None。
    """
    session = _make_session(answer=50)
    engine = _make_engine(session)
    await engine.timeout_guesser()
    assert len(session.guess_log) == 1, "timeout 後應有一筆 guess_log"
    entry = session.guess_log[0]
    assert entry["result"] == "timeout", f"result 應是 timeout，實際：{entry['result']!r}"
    assert entry["guess"] is None, "timeout 的 guess 應為 None"
    assert entry["guesser"] == "狗與露", f"guesser 應是 狗與露，實際：{entry['guesser']!r}"
