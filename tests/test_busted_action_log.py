"""TDD — Busted action_log

功能：每次 buzz_in / submit_answer / expire_buzz 後，
      session.action_log 累積一筆事件紀錄，
      _build_ws_state 回傳 action_log 供 Web UI 顯示歷程。

Tests:
  A) 初始 session.action_log 為空 list
  B) buzz_in 成功 → action_log 有 type="buzz" 一筆，含 guesser_name / clue_round
  C) submit_answer 猜中 → action_log 有 type="correct" 一筆，含 answer / score
  D) submit_answer 猜錯 → action_log 有 type="wrong" 一筆，含 guess / matched_chars
  E) expire_buzz → action_log 有 type="timeout" 一筆，含 guesser_name
  F) 多次事件 → action_log 依序累積（不蓋掉）
  G) _build_ws_state 包含 action_log 欄位
  H) action_log 超過 200 筆 → 自動截斷保留最新 200
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from game.session import GameSession, GameState, PlayerState
from game.engine import GameEngine


# ─── helpers ─────────────────────────────────────────────────────────────────

def _make_session(state: GameState = GameState.CLUE_ACTIVE) -> GameSession:
    s = GameSession.__new__(GameSession)
    s.session_id = "t"
    s.guild_id = 1
    s.channel_id = 1
    s.players = [
        PlayerState(user_id="u1", display_name="狗與露"),
        PlayerState(user_id="u2", display_name="Showay"),
        PlayerState(user_id="setter", display_name="出題人"),
    ]
    s.state = state
    s.current_setter_id = "setter"
    s.current_answer = "巨石強森"
    s.current_clues = ["他是演員", "他很壯"]
    s.current_round = 2
    s.buzz_holder_id = None
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
    s.action_log = []
    return s


def _make_engine(session: GameSession) -> GameEngine:
    return GameEngine(
        session=session,
        on_state_change=AsyncMock(),
        db_path=":memory:",
    )


# ─── A: 初始為空 ──────────────────────────────────────────────────────────────

def test_session_action_log_initial_empty():
    """A: 新建 session 時 action_log 為空 list"""
    s = GameSession(session_id="x", guild_id=1, channel_id=1)
    assert hasattr(s, "action_log"), "GameSession 必須有 action_log 屬性"
    assert s.action_log == []


# ─── B: buzz_in → type=buzz ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_buzz_in_appends_action_log():
    """B: buzz_in 成功 → action_log 有一筆 type=buzz"""
    session = _make_session(GameState.CLUE_ACTIVE)
    engine = _make_engine(session)
    await engine.buzz_in("u1")
    assert len(session.action_log) == 1
    entry = session.action_log[0]
    assert entry["type"] == "buzz"
    assert entry["guesser_name"] == "狗與露"
    assert "clue_round" in entry


# ─── C: submit_answer 猜中 → type=correct ───────────────────────────────────

@pytest.mark.asyncio
async def test_submit_answer_correct_appends_action_log():
    """C: 猜中 → action_log 有 type=correct，含 answer / score"""
    session = _make_session(GameState.BUZZ_LOCKED)
    session.buzz_holder_id = "u1"
    engine = _make_engine(session)
    await engine.submit_answer("u1", "巨石強森")
    correct_entries = [e for e in session.action_log if e["type"] == "correct"]
    assert len(correct_entries) == 1
    e = correct_entries[0]
    assert e["guesser_name"] == "狗與露"
    assert e["answer"] == "巨石強森"
    assert "score" in e


# ─── D: submit_answer 猜錯 → type=wrong ─────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_answer_wrong_appends_action_log():
    """D: 猜錯 → action_log 有 type=wrong，含 guess / matched_chars"""
    session = _make_session(GameState.BUZZ_LOCKED)
    session.buzz_holder_id = "u1"
    engine = _make_engine(session)
    await engine.submit_answer("u1", "約翰希南")
    wrong_entries = [e for e in session.action_log if e["type"] == "wrong"]
    assert len(wrong_entries) == 1
    e = wrong_entries[0]
    assert e["guesser_name"] == "狗與露"
    assert e["guess"] == "約翰希南"
    assert "matched_chars" in e


# ─── E: expire_buzz → type=timeout ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_expire_buzz_appends_action_log():
    """E: expire_buzz → action_log 有 type=timeout，含 guesser_name"""
    session = _make_session(GameState.BUZZ_LOCKED)
    session.buzz_holder_id = "u1"
    engine = _make_engine(session)
    await engine.expire_buzz()
    assert len(session.action_log) == 1
    e = session.action_log[0]
    assert e["type"] == "timeout"
    assert e["guesser_name"] == "狗與露"


# ─── F: 多次事件 → 依序累積 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_action_log_accumulates_in_order():
    """F: buzz → wrong → buzz → 三筆依序累積"""
    session = _make_session(GameState.CLUE_ACTIVE)
    engine = _make_engine(session)
    await engine.buzz_in("u1")
    # 手動設 BUZZ_LOCKED 狀態讓 submit_answer 可以執行
    session.state = GameState.BUZZ_LOCKED
    session.buzz_holder_id = "u1"
    await engine.submit_answer("u1", "約翰希南")  # wrong
    # 等 state 回 CLUE_ACTIVE，再 buzz
    await engine.buzz_in("u2")
    assert len(session.action_log) == 3
    assert session.action_log[0]["type"] == "buzz"
    assert session.action_log[1]["type"] == "wrong"
    assert session.action_log[2]["type"] == "buzz"


# ─── G: _build_ws_state 包含 action_log ──────────────────────────────────────

def test_build_ws_state_includes_action_log():
    """G: _build_ws_state 必須含 action_log 欄位"""
    from cogs.game_cog import BustedCog
    bot = MagicMock()
    bot.cogs.get.return_value = None
    bot.voice_clients = []
    cog = BustedCog(bot)
    session = _make_session(GameState.CLUE_ACTIVE)
    session.action_log = [
        {"type": "buzz", "guesser_name": "狗與露", "clue_round": 2, "round_num": 1},
    ]
    cog._session = session
    state = cog._build_ws_state(session)
    assert "action_log" in state
    assert len(state["action_log"]) == 1
    assert state["action_log"][0]["guesser_name"] == "狗與露"


# ─── H: 超過 200 筆自動截斷 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_action_log_capped_at_200():
    """H: action_log 超過 200 筆後截斷到最新 200"""
    session = _make_session(GameState.BUZZ_LOCKED)
    session.buzz_holder_id = "u1"
    # 預填 200 筆
    session.action_log = [{"type": "dummy", "n": i} for i in range(200)]
    engine = _make_engine(session)
    await engine.submit_answer("u1", "約翰希南")  # wrong
    assert len(session.action_log) == 200
    # 最後一筆應是剛加入的 wrong
    assert session.action_log[-1]["type"] == "wrong"
