"""TDD — Busted99LLMEngine: _ok 驗證防止 LLM 幻覺

Bug: 現在的 _ok check 允許 LLM 回傳 last_wrong 而不驗證 space <= 2，
     導致 space > 2 時 LLM 如果幻覺 last_wrong，猜題人會拿到不應得的 100 分。

Tests:
  A) LLM 回 last_wrong 但 space=10（> 2） → fallback code 規則，不給 100 分
  B) LLM 回 last_wrong 且 space=2，number != answer → 接受，猜題人得 100
  C) LLM 回 last_wrong 且 space=2，number == answer → 矛盾，fallback 為 last_bust
  D) LLM 回 bust 但 number != answer → 矛盾，fallback 為 wrong_low/wrong_high
"""

from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from game.busted99.session import Busted99Session, Busted99State, Player99State


def _make_session(low: int, high: int, answer: int, guesser: str = "u1") -> Busted99Session:
    s = Busted99Session.__new__(Busted99Session)
    s.session_id = "test"
    s.guild_id = 1
    s.channel_id = 1
    s.players = [
        Player99State(user_id="u1", display_name="狗與露", score=0),
        Player99State(user_id="u2", display_name="Marvin", score=0),
    ]
    s.state = Busted99State.GUESSING
    s.setter_id = "u2"
    s.answer = answer
    s.low_bound = low
    s.high_bound = high
    s.current_guesser_id = guesser
    s.guesser_order = [guesser]
    s.guessing_queue = []
    s.round_num = 1
    s.game_message_id = None
    s.started_at = 0.0
    s.last_guess = None
    s.last_guess_result = None
    return s


def _mock_llm_response(outcome: str, narration: str = "test") -> AsyncMock:
    """回傳固定 JSON 的 mock LLM client。"""
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = json.dumps({"outcome": outcome, "narration": narration})
    client = MagicMock()
    client.chat = MagicMock()
    client.chat.completions = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=resp)
    return client


def _make_engine(session: Busted99Session, llm_client=None):
    from game.busted99.llm_engine import Busted99LLMEngine
    return Busted99LLMEngine(
        session=session,
        db_path=":memory:",
        on_state_change=AsyncMock(),
        llm_client=llm_client,
    )


# ─── A: LLM 幻覺 last_wrong 但 space > 2 → fallback，不給 100 分 ──────────────

@pytest.mark.asyncio
async def test_llm_last_wrong_rejected_when_space_gt2():
    """
    space=10，answer=50，number=30（wrong_low 正確答案）。
    LLM 幻覺 last_wrong → _ok 應拒絕，fallback code 算 wrong_low，u1 不得分。
    """
    session = _make_session(low=45, high=55, answer=50)  # space=11
    client = _mock_llm_response("last_wrong")
    engine = _make_engine(session, llm_client=client)

    result = await engine.submit_guess("u1", 47)  # 47 < 50 → should be wrong_low
    assert result["result"] != "last_wrong", (
        "space=11 時 LLM 幻覺 last_wrong 應被 _ok 拒絕，fallback 給 wrong_low"
    )
    u1 = next(p for p in session.players if p.user_id == "u1")
    assert u1.score == 0, "wrong_low 猜題人不應得分（不是 last_wrong）"


# ─── B: LLM 回 last_wrong，space=2，number != answer → 正確接受 ────────────────

@pytest.mark.asyncio
async def test_llm_last_wrong_accepted_when_space_eq2_correct():
    """
    space=2（25-26），answer=25，number=26（猜錯，應得 100）。
    LLM 回 last_wrong → 合法，u1 得 100 分。
    """
    session = _make_session(low=25, high=26, answer=25)  # space=2
    client = _mock_llm_response("last_wrong", "最後猜錯反得分！")
    engine = _make_engine(session, llm_client=client)

    result = await engine.submit_guess("u1", 26)  # 26 != 25, space=2 → last_wrong
    assert result["result"] == "last_wrong", "space=2，猜錯應為 last_wrong"
    u1 = next(p for p in session.players if p.user_id == "u1")
    assert u1.score == 100, "last_wrong 猜題人應得 100 分"


# ─── C: LLM 回 last_wrong 但 number == answer（矛盾）→ fallback last_bust ──────

@pytest.mark.asyncio
async def test_llm_last_wrong_rejected_when_number_equals_answer():
    """
    space=2，answer=25，number=25（猜中！）。
    LLM 若亂回 last_wrong → 矛盾，_ok 應拒絕，code fallback → last_bust。
    """
    session = _make_session(low=25, high=26, answer=25)
    client = _mock_llm_response("last_wrong")  # LLM 幻覺
    engine = _make_engine(session, llm_client=client)

    result = await engine.submit_guess("u1", 25)  # 猜中，應 last_bust
    assert result["result"] == "last_bust", (
        "number == answer 時 last_wrong 應被拒，fallback 為 last_bust"
    )
    u1 = next(p for p in session.players if p.user_id == "u1")
    assert u1.score == 0, "last_bust 猜題人得 0 分"


# ─── D: LLM 回 bust 但 number != answer → fallback ──────────────────────────

@pytest.mark.asyncio
async def test_llm_bust_rejected_when_number_not_equal_answer():
    """
    LLM 幻覺 bust 但 number=30，answer=50 → 矛盾，應 fallback wrong_low。
    """
    session = _make_session(low=1, high=99, answer=50)
    client = _mock_llm_response("bust")
    engine = _make_engine(session, llm_client=client)

    result = await engine.submit_guess("u1", 30)  # 30 < 50 → wrong_low
    assert result["result"] == "wrong_low", (
        "LLM 幻覺 bust 但 number!=answer，應 fallback 為 wrong_low"
    )
    assert session.state == Busted99State.GUESSING, "wrong_low 後 state 仍是 GUESSING"
