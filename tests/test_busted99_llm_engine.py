"""TDD — Busted99 LLM Engine

驗項：
A) 猜中答案 → result=bust，state=GAME_OVER，非猜題人分數增加
B) 猜太高 → result=wrong_high，high_bound 更新為 guess-1
C) 猜太低 → result=wrong_low，low_bound 更新為 guess+1
D) LLM 嘗試在 delta 外修改分數或亂設 bounds → 無效，code 規則計算
E) space > 2 猜邊界 → result=boundary，state 不變
F) result dict 包含非空 narration 字串
G) LLM 回傳非 JSON → fallback 用 code 規則，不拋例外
H) out_of_range：code 在呼叫 LLM 前就拒絕（不消耗 LLM token）
I) last_bust：space ≤ 2 猜中 → setter 得 100，猜題人得 0
J) last_wrong：space ≤ 2 猜錯 → 猜題人得 100，遊戲結束
"""

from __future__ import annotations

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from game.busted99.session import Busted99Session, Busted99State, Player99State
from game.busted99.scoring import score_for_space


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_session(low: int = 1, high: int = 99, answer: int = 42, guesser: str = "u1") -> Busted99Session:
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
    s.guesser_order = [guesser]  # 固定順序（單一 guesser 場景）
    s.guessing_queue = []
    s.round_num = 1
    s.game_message_id = None
    s.started_at = 0.0
    s.last_guess = None
    s.last_guess_result = None
    return s


def _make_engine(session: Busted99Session):
    from game.busted99.llm_engine import Busted99LLMEngine
    return Busted99LLMEngine(
        session=session,
        db_path=":memory:",
        on_state_change=AsyncMock(),
    )


def _mock_llm(engine, outcome: str, new_low: int, new_high: int, narration: str) -> None:
    payload = json.dumps(
        {"outcome": outcome, "new_low": new_low, "new_high": new_high, "narration": narration},
        ensure_ascii=False,
    )
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content=payload))]
    engine._llm_client = MagicMock()
    engine._llm_client.chat.completions.create = AsyncMock(return_value=mock_resp)


# ── A: bust ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_guess_correct_triggers_bust():
    s = _make_session(low=1, high=99, answer=42, guesser="u1")
    engine = _make_engine(s)
    _mock_llm(engine, "bust", 1, 99, "你猜中了！Bust！")

    result = await engine.submit_guess("u1", 42)

    assert result["result"] == "bust"
    assert s.state == Busted99State.GAME_OVER
    u2 = next(p for p in s.players if p.user_id == "u2")
    assert u2.score == score_for_space(99)  # non-guesser 得分
    u1 = next(p for p in s.players if p.user_id == "u1")
    assert u1.score == 0  # 猜題人 bust，得 0


# ── B: wrong_high ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_guess_too_high_updates_high_bound():
    s = _make_session(low=1, high=99, answer=42, guesser="u1")
    engine = _make_engine(s)
    _mock_llm(engine, "wrong_high", 1, 55, "55 太高了！")

    result = await engine.submit_guess("u1", 55)

    assert result["result"] == "wrong_high"
    assert result["new_high"] == 55  # 對齊原 engine：bound = guess
    assert s.high_bound == 55
    assert s.low_bound == 1
    assert s.state == Busted99State.GUESSING


# ── C: wrong_low ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_guess_too_low_updates_low_bound():
    s = _make_session(low=1, high=99, answer=42, guesser="u1")
    engine = _make_engine(s)
    _mock_llm(engine, "wrong_low", 30, 99, "30 太低了！")

    result = await engine.submit_guess("u1", 30)

    assert result["result"] == "wrong_low"
    assert result["new_low"] == 30
    assert s.low_bound == 30
    assert s.high_bound == 99
    assert s.state == Busted99State.GUESSING


# ── D: LLM 嘗試修改分數被忽略 ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_score_field_in_response_is_ignored():
    s = _make_session(low=1, high=99, answer=42, guesser="u1")
    engine = _make_engine(s)

    # LLM 回傳含 scores 欄位（試圖注入）+ 正確的 bust
    payload = json.dumps({
        "outcome": "bust",
        "new_low": 1, "new_high": 99,
        "narration": "Bust！",
        "scores": {"u1": 9999, "u2": 9999},
    }, ensure_ascii=False)
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content=payload))]
    engine._llm_client = MagicMock()
    engine._llm_client.chat.completions.create = AsyncMock(return_value=mock_resp)

    await engine.submit_guess("u1", 42)

    for p in s.players:
        assert p.score < 9999, f"{p.display_name} 分數不應被 LLM 任意設定"


@pytest.mark.asyncio
async def test_llm_malicious_bounds_ignored():
    """LLM 回傳 wrong_low + new_low=99（試圖瞬間縮空間）→ 應被忽略，bound 用 code 規則"""
    s = _make_session(low=1, high=99, answer=42, guesser="u1")
    engine = _make_engine(s)
    # LLM 想把 low_bound 拉到 99（惡意）
    _mock_llm(engine, "wrong_low", 99, 99, "太低了")
    await engine.submit_guess("u1", 30)
    assert s.low_bound == 30, "low_bound 應該等於 guess，不是 LLM 任意值"


# ── 3-layer fallback (Cerebras → Groq → Gemini → code) ──────────────────────

@pytest.mark.asyncio
async def test_fallback_cerebras_fails_groq_succeeds():
    """Cerebras 429 → Groq 接手回 outcome。"""
    from game.busted99.llm_engine import Busted99LLMEngine
    s = _make_session(low=1, high=99, answer=42, guesser="u1")
    engine = Busted99LLMEngine(session=s, db_path=":memory:", on_state_change=AsyncMock())

    # mock client builders：cerebras 拋 429，groq 成功
    cerebras_mock = MagicMock()
    cerebras_mock.chat.completions.create = AsyncMock(side_effect=Exception("429 rate limited"))
    groq_mock = MagicMock()
    groq_resp = MagicMock()
    groq_resp.choices = [MagicMock(message=MagicMock(content=json.dumps(
        {"outcome": "wrong_high", "narration": "groq 來救"}
    )))]
    groq_mock.chat.completions.create = AsyncMock(return_value=groq_resp)

    engine._get_cerebras_client = lambda: cerebras_mock
    engine._get_groq_client = lambda: groq_mock
    engine._get_gemini_client = lambda: None  # Gemini 不該被叫到

    result = await engine._call_llm(1, 99, 55, "狗與露")
    assert result["outcome"] == "wrong_high"
    assert "groq" in result["narration"]
    # Gemini 不該被叫
    groq_mock.chat.completions.create.assert_called_once()


@pytest.mark.asyncio
async def test_fallback_cerebras_and_groq_fail_gemini_succeeds():
    """Cerebras + Groq 都掛 → Gemini 接手。"""
    from game.busted99.llm_engine import Busted99LLMEngine
    s = _make_session(low=1, high=99, answer=42, guesser="u1")
    engine = Busted99LLMEngine(session=s, db_path=":memory:", on_state_change=AsyncMock())

    fail_mock = MagicMock()
    fail_mock.chat.completions.create = AsyncMock(side_effect=Exception("down"))

    gemini_mock = MagicMock()
    gemini_resp = MagicMock()
    gemini_resp.text = json.dumps({"outcome": "wrong_low", "narration": "gemini 來救"})
    gemini_mock.aio.models.generate_content = AsyncMock(return_value=gemini_resp)

    engine._get_cerebras_client = lambda: fail_mock
    engine._get_groq_client = lambda: fail_mock
    engine._get_gemini_client = lambda: gemini_mock

    result = await engine._call_llm(1, 99, 30, "狗與露")
    assert result["outcome"] == "wrong_low"
    assert "gemini" in result["narration"]


@pytest.mark.asyncio
async def test_fallback_all_three_fail_returns_none():
    """3 層全掛 → 回 None，讓 submit_guess 用 _adjudicate。"""
    from game.busted99.llm_engine import Busted99LLMEngine
    s = _make_session(low=1, high=99, answer=42, guesser="u1")
    engine = Busted99LLMEngine(session=s, db_path=":memory:", on_state_change=AsyncMock())

    fail_oai = MagicMock()
    fail_oai.chat.completions.create = AsyncMock(side_effect=Exception("down"))
    fail_gemini = MagicMock()
    fail_gemini.aio.models.generate_content = AsyncMock(side_effect=Exception("down"))

    engine._get_cerebras_client = lambda: fail_oai
    engine._get_groq_client = lambda: fail_oai
    engine._get_gemini_client = lambda: fail_gemini

    result = await engine._call_llm(1, 99, 55, "狗與露")
    assert result is None


# ── LLM 信任：LLM 是判定的權威（設計上 LLM-first）─────────────────────────────

@pytest.mark.asyncio
async def test_llm_outcome_is_trusted():
    """LLM 是規則裁判：它說 last_wrong 就是 last_wrong，code 不擅自覆蓋。
    (依賴 few-shot prompt 讓 LLM 不誤判邊界 case)"""
    s = _make_session(low=25, high=26, answer=25, guesser="u1")
    engine = _make_engine(s)
    _mock_llm(engine, "last_wrong", 25, 26, "神運氣！")

    result = await engine.submit_guess("u1", 26)

    assert result["result"] == "last_wrong"
    assert s.state == Busted99State.GAME_OVER
    guesser = next(p for p in s.players if p.user_id == "u1")
    assert guesser.score == 100


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_code_adjudicate():
    """LLM 完全失敗（return None）才 fallback 到 code，避免遊戲卡死。"""
    s = _make_session(low=25, high=26, answer=25, guesser="u1")
    engine = _make_engine(s)
    # mock LLM 失敗（return None）
    engine._llm_client = MagicMock()
    engine._llm_client.chat.completions.create = AsyncMock(side_effect=ConnectionError("down"))

    result = await engine.submit_guess("u1", 26)
    # code fallback 算 last_wrong（space=2, guess!=answer）
    assert result["result"] == "last_wrong"


# ── wrong_low/high 後必須 advance + on_state_change ──────────────────────────

@pytest.mark.asyncio
async def test_wrong_high_advances_guesser_and_notifies():
    """猜錯後必須 _advance_guesser + on_state_change，否則 Marvin 卡死。"""
    s = _make_session(low=1, high=99, answer=42, guesser="u1")
    s.guessing_queue = []  # u1 是唯一 guesser，advance 後重建 queue
    on_change = AsyncMock()
    from game.busted99.llm_engine import Busted99LLMEngine
    engine = Busted99LLMEngine(session=s, db_path=":memory:", on_state_change=on_change)
    _mock_llm(engine, "wrong_high", 1, 55, "x")

    await engine.submit_guess("u1", 55)

    assert s.high_bound == 55, "wrong_high 應更新 high_bound"
    assert s.round_num == 2, "queue 空 → advance 應推進 round_num"
    on_change.assert_called_once(), "guesser 換了必須通知 cog（否則下一輪 task 不會 spawn）"


@pytest.mark.asyncio
async def test_boundary_does_not_advance():
    """boundary 不消耗回合，不該 advance / notify。"""
    s = _make_session(low=30, high=70, answer=50, guesser="u1")
    on_change = AsyncMock()
    from game.busted99.llm_engine import Busted99LLMEngine
    engine = Busted99LLMEngine(session=s, db_path=":memory:", on_state_change=on_change)
    _mock_llm(engine, "boundary", 30, 70, "x")

    await engine.submit_guess("u1", 30)
    on_change.assert_not_called()
    assert s.current_guesser_id == "u1"


# ── 3-player last_bust 對齊原 engine ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_last_bust_other_players_also_get_points():
    """last_bust 時，非 setter 非 guesser 的其他玩家也要得 score_for_space(space)"""
    from game.busted99.scoring import score_for_space
    s = _make_session(low=42, high=43, answer=42, guesser="u1")
    # 加第三個玩家（非 setter 非 guesser）
    s.players.append(Player99State(user_id="u3", display_name="第三人", score=0))
    engine = _make_engine(s)
    _mock_llm(engine, "last_bust", 42, 43, "最後機會 bust！")

    await engine.submit_guess("u1", 42)

    setter = next(p for p in s.players if p.user_id == "u2")
    third = next(p for p in s.players if p.user_id == "u3")
    guesser = next(p for p in s.players if p.user_id == "u1")
    assert setter.score == 100
    assert third.score == score_for_space(2)  # space=2
    assert guesser.score == 0


# ── 分數持久化：傳的是 delta 不是 session total ─────────────────────────────

@pytest.mark.asyncio
async def test_score_persistence_uses_delta_not_session_total():
    """_write_score_deltas 必須收到 round delta，不是 session 累積分。"""
    from unittest.mock import patch as _patch
    s = _make_session(low=1, high=99, answer=42, guesser="u1")
    # 模擬玩家在 session 已累積分數（多輪場景或重構後的潛在風險）
    next(p for p in s.players if p.user_id == "u2").score = 50
    engine = _make_engine(s)
    _mock_llm(engine, "bust", 1, 99, "Bust！")

    captured: list[list[tuple[str, str, int]]] = []
    with _patch.object(engine, "_write_score_deltas", side_effect=lambda d: captured.append(d)):
        await engine.submit_guess("u1", 42)
        # 等 executor flush
        await asyncio.sleep(0.05)

    assert captured, "應有 delta 寫入"
    deltas = captured[0]
    u2_delta = next((d for u, _, d in deltas if u == "u2"), None)
    assert u2_delta == score_for_space(99), f"u2 delta 應為本輪得分，不是 50+本輪"


# ── TOCTOU：LLM 呼叫期間 state 改變 ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_state_change_during_llm_call_aborts():
    """LLM 跑到一半 state 變 GAME_OVER（例如 timeout）→ submit_guess 不應寫入。"""
    s = _make_session(low=1, high=99, answer=42, guesser="u1")
    engine = _make_engine(s)

    # 攔截 LLM call：在裡面把 state 改成 GAME_OVER（模擬 timeout 觸發）
    async def _evil_llm(low, high, guess, name):
        s.state = Busted99State.GAME_OVER
        return {"outcome": "wrong_low", "narration": "x"}
    engine._call_llm = _evil_llm

    result = await engine.submit_guess("u1", 30)
    assert result["result"] == "invalid_state"
    assert s.low_bound == 1, "state 已變，bounds 不應被改"


# ── E: boundary ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_boundary_guess_rejected_when_space_gt_2():
    s = _make_session(low=30, high=70, answer=50, guesser="u1")
    engine = _make_engine(s)
    _mock_llm(engine, "boundary", 30, 70, "不能猜邊界！")

    result = await engine.submit_guess("u1", 30)

    assert result["result"] == "boundary"
    assert s.state == Busted99State.GUESSING
    assert s.low_bound == 30 and s.high_bound == 70


# ── F: narration ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_result_contains_nonempty_narration():
    s = _make_session()
    engine = _make_engine(s)
    _mock_llm(engine, "wrong_high", 1, 41, "Marvin 說：太高了！")

    result = await engine.submit_guess("u1", 42)

    assert "narration" in result
    assert isinstance(result["narration"], str) and len(result["narration"]) > 0


# ── G: JSON parse error fallback ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_garbage_response_falls_back_gracefully():
    s = _make_session(low=1, high=99, answer=42, guesser="u1")
    engine = _make_engine(s)

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content="這不是 JSON！哈哈哈"))]
    engine._llm_client = MagicMock()
    engine._llm_client.chat.completions.create = AsyncMock(return_value=mock_resp)

    result = await engine.submit_guess("u1", 55)

    assert "result" in result
    assert result["result"] != "error"  # 不應拋出，要有合法 fallback 結果


# ── H: out_of_range（LLM 不呼叫）────────────────────────────────────────────

@pytest.mark.asyncio
async def test_out_of_range_skips_llm_call():
    s = _make_session(low=30, high=70, answer=50, guesser="u1")
    engine = _make_engine(s)
    engine._llm_client = MagicMock()
    engine._llm_client.chat.completions.create = AsyncMock()

    result = await engine.submit_guess("u1", 10)  # 10 < low_bound=30

    assert result["result"] == "out_of_range"
    engine._llm_client.chat.completions.create.assert_not_called()


# ── I: last_bust（space ≤ 2，猜中）───────────────────────────────────────────

@pytest.mark.asyncio
async def test_last_bust_setter_gets_100():
    s = _make_session(low=42, high=43, answer=42, guesser="u1")
    engine = _make_engine(s)
    _mock_llm(engine, "last_bust", 42, 43, "最後機會！Bust！")

    result = await engine.submit_guess("u1", 42)

    assert result["result"] == "last_bust"
    assert s.state == Busted99State.GAME_OVER
    setter = next(p for p in s.players if p.user_id == "u2")
    assert setter.score == 100  # setter 得 100
    guesser = next(p for p in s.players if p.user_id == "u1")
    assert guesser.score == 0  # 猜題人得 0


# ── J: last_wrong（space ≤ 2，猜錯）─────────────────────────────────────────

@pytest.mark.asyncio
async def test_last_wrong_guesser_gets_100():
    s = _make_session(low=42, high=43, answer=42, guesser="u1")
    engine = _make_engine(s)
    _mock_llm(engine, "last_wrong", 42, 43, "猜錯了但你贏了！")

    result = await engine.submit_guess("u1", 43)

    assert result["result"] == "last_wrong"
    assert s.state == Busted99State.GAME_OVER
    guesser = next(p for p in s.players if p.user_id == "u1")
    assert guesser.score == 100  # 猜題人得 100
