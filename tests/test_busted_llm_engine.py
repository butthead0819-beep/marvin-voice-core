"""TDD — GameLLMEngine(GameEngine)

功能：繼承 GameEngine，覆寫 submit_answer()。
  - 用 LLM 一次判定 correct + 生成 narration
  - 分數計算、state 轉換完全繼承 GameEngine（code 不重寫）
  - LLM 判定 `correct` 必須過 code 交叉驗證（防幻覺）
  - LLM 失敗 → fallback 到 code judge，narration = ""
  - Round 5 仍走 partial_score 路徑，不呼叫 LLM

Tests:
  A) LLM 回 correct=True → result["correct"] is True
  B) LLM 回 correct=False → result["correct"] is False
  C) LLM 回 narration → result["narration"] == narration
  D) LLM 失敗（None） → fallback code judge，result["narration"] == ""
  E) LLM 判 correct=True 但 answer != guess → code override → correct=False
  F) correct=True → state 變 ROUND_RESULT
  G) correct=False → state 仍 CLUE_ACTIVE（搶答視窗恢復）
  H) Round 5 partial_score 路徑 → 不呼叫 LLM，result 無 narration 或 narration=""
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from game.session import GameSession, GameState, PlayerState
from game.busted_llm_engine import GameLLMEngine


# ─── helpers ─────────────────────────────────────────────────────────────────

def _make_session(clue_round: int = 1, answer: str = "巨石強森") -> GameSession:
    s = GameSession.__new__(GameSession)
    s.session_id = "t"
    s.guild_id = 1
    s.channel_id = 1
    s.players = [
        PlayerState(user_id="u1", display_name="狗與露"),
        PlayerState(user_id="setter", display_name="出題人"),
    ]
    s.state = GameState.BUZZ_LOCKED
    s.current_setter_id = "setter"
    s.current_answer = answer
    s.current_clues = ["第一條線索"]
    s.current_round = clue_round
    s.buzz_holder_id = "u1"
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


def _make_engine(session: GameSession, llm_client=None) -> GameLLMEngine:
    return GameLLMEngine(
        session=session,
        on_state_change=AsyncMock(),
        db_path=":memory:",
        llm_client=llm_client,
    )


def _mock_llm(correct: bool, narration: str = "測試播報"):
    """Mock client that returns {correct, narration} via chat.completions.create."""
    import json
    client = MagicMock()
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = json.dumps({"correct": correct, "narration": narration})
    client.chat.completions.create = AsyncMock(return_value=response)
    return client


# ─── A: LLM correct=True ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_engine_correct_true_returns_correct():
    """A: LLM 判 correct=True → result['correct'] is True"""
    session = _make_session(answer="巨石強森")
    engine = _make_engine(session, llm_client=_mock_llm(correct=True))
    result = await engine.submit_answer("u1", "巨石強森")
    assert result["correct"] is True


# ─── B: LLM correct=False ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_engine_correct_false_returns_correct():
    """B: LLM 判 correct=False → result['correct'] is False"""
    session = _make_session(answer="巨石強森")
    engine = _make_engine(session, llm_client=_mock_llm(correct=False))
    result = await engine.submit_answer("u1", "巨石龍")
    assert result["correct"] is False


# ─── C: narration 回到 result ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_engine_narration_in_result():
    """C: LLM 回 narration → result['narration'] == 該字串"""
    session = _make_session(answer="巨石強森")
    engine = _make_engine(session, llm_client=_mock_llm(correct=True, narration="猜中了！全場最強！"))
    result = await engine.submit_answer("u1", "巨石強森")
    assert result.get("narration") == "猜中了！全場最強！"


# ─── D: LLM 失敗 → code fallback ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_engine_fallback_on_llm_failure():
    """D: LLM 拋例外 → fallback code judge，narration="""""
    session = _make_session(answer="巨石強森")
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=Exception("timeout"))
    engine = _make_engine(session, llm_client=client)
    result = await engine.submit_answer("u1", "巨石強森")  # 答案完全一致 → code fallback correct
    assert result["correct"] is True
    assert result.get("narration", "") == ""


# ─── E: LLM 幻覺：correct=True 但答案不符 → code override ──────────────────

@pytest.mark.asyncio
async def test_llm_engine_hallu_correct_overridden():
    """E: LLM 說 correct=True 但 guess != answer → code 判 False，保護分數不亂送"""
    session = _make_session(answer="巨石強森")
    engine = _make_engine(session, llm_client=_mock_llm(correct=True))
    result = await engine.submit_answer("u1", "完全錯誤的答案")
    # code 說 False → 覆蓋 LLM
    assert result["correct"] is False


# ─── F: correct=True → state → ROUND_RESULT ──────────────────────────────────

@pytest.mark.asyncio
async def test_llm_engine_correct_sets_round_result():
    """F: 猜中 → state 變 ROUND_RESULT"""
    session = _make_session(answer="巨石強森")
    engine = _make_engine(session, llm_client=_mock_llm(correct=True))
    await engine.submit_answer("u1", "巨石強森")
    assert session.state == GameState.ROUND_RESULT


# ─── G: correct=False → state → CLUE_ACTIVE ─────────────────────────────────

@pytest.mark.asyncio
async def test_llm_engine_wrong_sets_clue_active():
    """G: 猜錯 → state 回 CLUE_ACTIVE"""
    session = _make_session(answer="巨石強森")
    engine = _make_engine(session, llm_client=_mock_llm(correct=False))
    await engine.submit_answer("u1", "錯誤答案")
    assert session.state == GameState.CLUE_ACTIVE


# ─── H: Round 5 → partial score，不呼叫 LLM ──────────────────────────────────

@pytest.mark.asyncio
async def test_llm_engine_round5_no_llm():
    """H: Round 5 走 partial_score，LLM 不被呼叫，narration absent or empty"""
    session = _make_session(clue_round=5, answer="巨石強森")
    client = MagicMock()
    client.chat.completions.create = AsyncMock()
    engine = _make_engine(session, llm_client=client)
    result = await engine.submit_answer("u1", "巨石")
    client.chat.completions.create.assert_not_called()
    assert result.get("narration", "") == ""
