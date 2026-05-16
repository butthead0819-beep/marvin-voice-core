"""TDD — Busted99Cog.receive_voice_answer_by_speaker 路由邏輯

驗項：
A) speaker 不是當前猜題人 → 不消耗（return False）
B) state 不是 GUESSING → 不消耗
C) LLM 抽到數字 → 提交，return True
D) LLM None + regex parse 抽到 → fallback 提交，return True
E) 全文無數字 → 不消耗（return False）
F) 引擎 None → 不消耗
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from game.busted99.session import Busted99Session, Busted99State, Player99State


def _make_bot():
    bot = MagicMock()
    bot.cogs.get.return_value = None
    bot.voice_clients = []
    return bot


def _make_cog_with_session(state: Busted99State = Busted99State.GUESSING):
    from cogs.busted99_cog import Busted99Cog
    cog = Busted99Cog(_make_bot())
    s = Busted99Session(session_id="t", guild_id=1, channel_id=1)
    s.players = [
        Player99State(user_id="u1", display_name="狗與露", score=0),
        Player99State(user_id="marvin", display_name="Marvin", score=0),
    ]
    s.state = state
    s.setter_id = "marvin"
    s.answer = 42
    s.low_bound = 1
    s.high_bound = 99
    s.current_guesser_id = "u1"
    cog._session = s
    cog._engine = AsyncMock()
    cog._engine.submit_guess = AsyncMock(return_value={"result": "wrong_high", "new_low": 1, "new_high": 49, "score_change": 0, "space": 49, "narration": ""})
    cog._channel = AsyncMock()
    return cog


# ── A: speaker 不是當前猜題人 ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_voice_speaker_not_current_guesser_returns_false():
    cog = _make_cog_with_session()
    with patch("cogs.busted99_cog.extract_guess_via_llm", AsyncMock(return_value=50)):
        ok = await cog.receive_voice_answer_by_speaker("Marvin", "我猜五十")
    assert ok is False
    cog._engine.submit_guess.assert_not_called()


# ── B: state 不是 GUESSING ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_voice_wrong_state_returns_false():
    cog = _make_cog_with_session(state=Busted99State.JOINING)
    with patch("cogs.busted99_cog.extract_guess_via_llm", AsyncMock(return_value=50)):
        ok = await cog.receive_voice_answer_by_speaker("狗與露", "我猜五十")
    assert ok is False


# ── C: LLM 抽到 → 提交 ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_voice_llm_extract_submits_guess():
    cog = _make_cog_with_session()
    with patch("cogs.busted99_cog.extract_guess_via_llm", AsyncMock(return_value=57)):
        ok = await cog.receive_voice_answer_by_speaker("狗與露", "我猜五十七")
    assert ok is True
    cog._engine.submit_guess.assert_called_once_with("u1", 57)


# ── D: LLM 失敗 → regex fallback ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_voice_regex_fallback_when_llm_returns_none():
    cog = _make_cog_with_session()
    with patch("cogs.busted99_cog.extract_guess_via_llm", AsyncMock(return_value=None)):
        ok = await cog.receive_voice_answer_by_speaker("狗與露", "我猜 38")
    assert ok is True
    cog._engine.submit_guess.assert_called_once_with("u1", 38)


# ── E: 完全沒數字 → False ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_voice_no_number_anywhere_returns_false():
    cog = _make_cog_with_session()
    with patch("cogs.busted99_cog.extract_guess_via_llm", AsyncMock(return_value=None)):
        ok = await cog.receive_voice_answer_by_speaker("狗與露", "嗯不知道耶")
    assert ok is False
    cog._engine.submit_guess.assert_not_called()


# ── F: engine None ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_voice_no_engine_returns_false():
    cog = _make_cog_with_session()
    cog._engine = None
    ok = await cog.receive_voice_answer_by_speaker("狗與露", "我猜五十")
    assert ok is False


# ── G: WebUI token 驗證 ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_web_action_without_token_is_dropped():
    """無 resolved_user_id（無 token）的 web action 必須靜默丟棄（P1 安全修復）。"""
    cog = _make_cog_with_session()
    await cog._handle_web_action({
        "type": "b99_guess", "name": "狗與露", "number": 50,
    })
    cog._engine.submit_guess.assert_not_called()


@pytest.mark.asyncio
async def test_web_action_unknown_name_silent_drop():
    """name 不在玩家清單 → 不該打 engine。"""
    cog = _make_cog_with_session()
    await cog._handle_web_action({
        "type": "b99_guess", "name": "陌生人", "number": 50,
    })
    cog._engine.submit_guess.assert_not_called()
