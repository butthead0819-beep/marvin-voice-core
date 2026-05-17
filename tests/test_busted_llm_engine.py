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
    """E: LLM 說 correct=True 但 guess 跟 answer 完全沒交集 → code 判 False"""
    session = _make_session(answer="巨石強森")
    engine = _make_engine(session, llm_client=_mock_llm(correct=True))
    result = await engine.submit_answer("u1", "完全錯誤的答案")
    # code 說 False → 覆蓋 LLM
    assert result["correct"] is False


# ─── E2: 部分匹配 — LLM 同意、code judge 該放行（與 prompt few-shot 一致）─

@pytest.mark.asyncio
async def test_llm_engine_substring_match_not_overridden():
    """Prompt 明寫「巨石 vs 巨石強森 可視情況算正確」、few-shot 範例就這樣標。
    code judge 不該因為「不等於」就 override 掉 LLM 的判定。"""
    session = _make_session(answer="巨石強森")
    engine = _make_engine(session, llm_client=_mock_llm(correct=True))
    # guess 是 answer 的子字串、≥2 字、≥ 半長 → 合理的部分匹配
    result = await engine.submit_answer("u1", "巨石")
    assert result["correct"] is True, "「巨石」對「巨石強森」是 prompt 認可的部分匹配，不該被 override"


@pytest.mark.asyncio
async def test_llm_engine_too_short_substring_still_overridden():
    """單字子字串（例如「強」對「巨石強森」）overlap 太低，code judge 該擋。"""
    session = _make_session(answer="巨石強森")
    engine = _make_engine(session, llm_client=_mock_llm(correct=True))
    result = await engine.submit_answer("u1", "強")  # 1 char, < min overlap 2
    assert result["correct"] is False


@pytest.mark.asyncio
async def test_llm_engine_substring_below_half_still_overridden():
    """子字串 < 半長 → 仍視為無意義匹配，override 為 False。"""
    session = _make_session(answer="巨石強森巨無霸")  # 7 chars
    engine = _make_engine(session, llm_client=_mock_llm(correct=True))
    result = await engine.submit_answer("u1", "巨石")  # 2 chars < 7/2 = 3.5
    assert result["correct"] is False


@pytest.mark.asyncio
async def test_llm_engine_extension_match_accepted():
    """玩家猜得比答案還長但包含答案（例如「巨石強森王」對「巨石強森」）→ 視為命中。"""
    session = _make_session(answer="巨石強森")
    engine = _make_engine(session, llm_client=_mock_llm(correct=True))
    result = await engine.submit_answer("u1", "巨石強森王")
    assert result["correct"] is True


# ─── Narration sanitization — prompt-injection defense ──────────────────────

@pytest.mark.asyncio
async def test_narration_capped_at_200_chars():
    """A 500-char hallucinated narration must reach the result <= 200 chars,
    otherwise the TTS queue can be flooded by a single prompt-injected guess."""
    session = _make_session(answer="巨石強森")
    long_narration = "啊" * 500
    engine = _make_engine(session, llm_client=_mock_llm(correct=True, narration=long_narration))
    result = await engine.submit_answer("u1", "巨石強森")
    assert "narration" in result
    assert len(result["narration"]) <= 200, (
        f"narration must be capped at 200 chars, got {len(result['narration'])}"
    )


@pytest.mark.asyncio
async def test_narration_strips_control_chars():
    """Control bytes (< 0x20 except newline) must be filtered before TTS / channel.send."""
    session = _make_session(answer="巨石強森")
    poisoned = "猜中\x01\x02了\x03"
    engine = _make_engine(session, llm_client=_mock_llm(correct=True, narration=poisoned))
    result = await engine.submit_answer("u1", "巨石強森")
    assert "\x01" not in result["narration"]
    assert "\x02" not in result["narration"]
    assert "\x03" not in result["narration"]
    # Visible text survives.
    assert "猜中" in result["narration"]
    assert "了" in result["narration"]


@pytest.mark.asyncio
async def test_narration_preserves_newlines():
    """Newlines (\\n) are allowed through — Marvin's multi-line narration is fine."""
    session = _make_session(answer="巨石強森")
    multi = "第一行\n第二行"
    engine = _make_engine(session, llm_client=_mock_llm(correct=True, narration=multi))
    result = await engine.submit_answer("u1", "巨石強森")
    assert "\n" in result["narration"]


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


# ─── I-K: 3-layer fallback chain (no injected llm_client → exercises _call_llm) ──

def _mock_openai_compat_client(*, returns=None, raises=None):
    """Build an AsyncOpenAI-shaped mock that either returns a JSON payload or raises."""
    import json as _json
    client = MagicMock()
    if raises is not None:
        client.chat.completions.create = AsyncMock(side_effect=raises)
    else:
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = _json.dumps(returns)
        client.chat.completions.create = AsyncMock(return_value=response)
    return client


def _mock_gemini_client(*, returns_text=None, raises=None):
    """Mimic google.genai client.aio.models.generate_content shape."""
    client = MagicMock()
    if raises is not None:
        client.aio.models.generate_content = AsyncMock(side_effect=raises)
    else:
        response = MagicMock()
        response.text = returns_text
        client.aio.models.generate_content = AsyncMock(return_value=response)
    return client


@pytest.mark.asyncio
async def test_fallback_cerebras_invalid_then_groq_wins():
    """I: Cerebras 回非 JSON → Groq 正常回應，採用 Groq 結果（不 fallthrough 到 Gemini）"""
    session = _make_session(answer="巨石強森")
    engine = _make_engine(session)
    cerebras = _mock_openai_compat_client(returns={"correct": "not-a-bool"})  # _parse_llm_response → None
    groq = _mock_openai_compat_client(returns={"correct": True, "narration": "groq 出馬"})
    gemini = _mock_gemini_client(returns_text='{"correct": false, "narration": "should not reach"}')
    engine._get_cerebras_client = lambda: cerebras
    engine._get_groq_client = lambda: groq
    engine._get_gemini_client = lambda: gemini

    result = await engine.submit_answer("u1", "巨石強森")
    assert result["correct"] is True
    assert result.get("narration") == "groq 出馬"
    gemini.aio.models.generate_content.assert_not_called()


@pytest.mark.asyncio
async def test_fallback_cerebras_raises_then_groq_wins():
    """J: Cerebras 拋例外 → 改用 Groq；Gemini 不會被呼叫"""
    session = _make_session(answer="巨石強森")
    engine = _make_engine(session)
    cerebras = _mock_openai_compat_client(raises=RuntimeError("cerebras down"))
    groq = _mock_openai_compat_client(returns={"correct": False, "narration": "groq 接手"})
    gemini = _mock_gemini_client(returns_text='{"correct": true}')
    engine._get_cerebras_client = lambda: cerebras
    engine._get_groq_client = lambda: groq
    engine._get_gemini_client = lambda: gemini

    result = await engine.submit_answer("u1", "錯的")
    assert result["correct"] is False
    gemini.aio.models.generate_content.assert_not_called()


@pytest.mark.asyncio
async def test_fallback_all_three_fail_uses_code_judge():
    """K: 三層全失敗 → 落到 code judge，narration=''"""
    session = _make_session(answer="巨石強森")
    engine = _make_engine(session)
    engine._get_cerebras_client = lambda: _mock_openai_compat_client(raises=RuntimeError("nope"))
    engine._get_groq_client = lambda: _mock_openai_compat_client(raises=RuntimeError("nope"))
    engine._get_gemini_client = lambda: _mock_gemini_client(raises=RuntimeError("nope"))

    # 答案完全相同 → code judge correct=True
    result = await engine.submit_answer("u1", "巨石強森")
    assert result["correct"] is True
    assert result.get("narration", "") == ""


@pytest.mark.asyncio
async def test_fallback_no_clients_configured_uses_code_judge():
    """L: 三個 client builder 全回 None（無 API key） → 直接 code judge"""
    session = _make_session(answer="巨石強森")
    engine = _make_engine(session)
    engine._get_cerebras_client = lambda: None
    engine._get_groq_client = lambda: None
    engine._get_gemini_client = lambda: None

    result = await engine.submit_answer("u1", "完全不同")
    assert result["correct"] is False
    assert result.get("narration", "") == ""
