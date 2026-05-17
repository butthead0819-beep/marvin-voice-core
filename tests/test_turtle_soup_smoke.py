"""整合 smoke test — engine + judge + voice_parse 端對端跑一場。

Mock LLM call 但走真實的 dispatch / state / SFX 排程邏輯。
驗證 A1（完整流程）、A5（state 防呆）、A6（SFX 序列）的整合。
"""
from __future__ import annotations
import asyncio
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock

from game.turtle_soup.engine import TurtleSoupEngine
from game.turtle_soup.session import (
    EndReason,
    TurtleSoupSession,
    TurtleSoupState,
)
from game.turtle_soup.puzzles import ELEVATOR_18F
from game.turtle_soup import llm_judge


@pytest.mark.asyncio
async def test_full_happy_path_win():
    """JOINING → PRESENTING → ASKING → 問 2 個問題 → 最終猜對 → WIN。"""
    states_seen: list[TurtleSoupState] = []

    async def cb(s):
        states_seen.append(s.state)

    session = TurtleSoupSession(session_id=str(uuid.uuid4()), guild_id=1, channel_id=1)
    eng = TurtleSoupEngine(session=session, puzzle=ELEVATOR_18F, on_state_change=cb)

    # 起點：IDLE
    assert eng.session.state == TurtleSoupState.IDLE

    # 啟動
    await eng.start_game()
    await eng.add_player("u1", "Alice")
    await eng.begin_presenting()
    await eng.begin_asking()

    # 模擬玩家問兩題（mock LLM 回 yes / no）
    eng_judge = llm_judge.judge_question
    llm_judge.judge_question = AsyncMock(side_effect=[
        {"verdict": "yes", "narration": "你抓到了", "_provider": "Cerebras"},
        {"verdict": "no", "narration": "想太多", "_provider": "Cerebras"},
    ])
    try:
        r1 = await eng.submit_question("u1", "Alice", "他是侏儒嗎？")
        r2 = await eng.submit_question("u1", "Alice", "電梯壞了嗎？")
    finally:
        llm_judge.judge_question = eng_judge

    assert r1["verdict"] == "yes"
    assert r2["verdict"] == "no"
    assert eng.session.questions_count == 2

    # 最終猜答（mock 接受）
    eng_final = llm_judge.judge_final_guess
    llm_judge.judge_final_guess = AsyncMock(return_value={
        "accepted": True, "covered_facts": [0, 1, 2],
        "narration": "想通了", "_provider": "Cerebras",
    })
    try:
        final = await eng.submit_final_guess("u1", "Alice", "他是侏儒按不到 22 樓按鈕")
    finally:
        llm_judge.judge_final_guess = eng_final

    assert final["accepted"] is True
    assert eng.session.state == TurtleSoupState.GAME_OVER
    assert eng.session.end_reason == EndReason.WIN

    # 應該收到 5 次 state 變動：JOINING / PRESENTING / ASKING / (submit_question 不變動) / GAME_OVER
    assert TurtleSoupState.JOINING in states_seen
    assert TurtleSoupState.PRESENTING in states_seen
    assert TurtleSoupState.ASKING in states_seen
    assert TurtleSoupState.GAME_OVER in states_seen


@pytest.mark.asyncio
async def test_full_happy_path_surrender():
    """中途投降。"""
    cb = AsyncMock()
    session = TurtleSoupSession(session_id=str(uuid.uuid4()), guild_id=1, channel_id=1)
    eng = TurtleSoupEngine(session=session, puzzle=ELEVATOR_18F, on_state_change=cb)

    await eng.start_game()
    await eng.add_player("u1", "Alice")
    await eng.begin_presenting()
    await eng.begin_asking()
    await eng.surrender("u1", "Alice")

    assert eng.session.state == TurtleSoupState.GAME_OVER
    assert eng.session.end_reason == EndReason.SURRENDER


@pytest.mark.asyncio
async def test_exhausted_path_after_max_questions():
    """達 max_questions 後自動結束。"""
    cb = AsyncMock()
    session = TurtleSoupSession(session_id=str(uuid.uuid4()), guild_id=1, channel_id=1)
    session.max_questions = 2
    eng = TurtleSoupEngine(session=session, puzzle=ELEVATOR_18F, on_state_change=cb)

    await eng.start_game()
    await eng.add_player("u1", "Alice")
    await eng.begin_presenting()
    await eng.begin_asking()

    orig = llm_judge.judge_question
    llm_judge.judge_question = AsyncMock(return_value={
        "verdict": "no", "narration": "x", "_provider": "Cerebras",
    })
    try:
        await eng.submit_question("u1", "Alice", "q1")
        await eng.submit_question("u1", "Alice", "q2")
    finally:
        llm_judge.judge_question = orig

    assert eng.session.state == TurtleSoupState.GAME_OVER
    assert eng.session.end_reason == EndReason.EXHAUSTED


@pytest.mark.asyncio
async def test_voice_parse_into_engine_integration():
    """voice_parse 輸出能餵進 engine：question / final_answer / surrender 三路徑。"""
    from game.turtle_soup.voice_parse import classify_intent

    cb = AsyncMock()
    session = TurtleSoupSession(session_id=str(uuid.uuid4()), guild_id=1, channel_id=1)
    eng = TurtleSoupEngine(session=session, puzzle=ELEVATOR_18F, on_state_change=cb)
    await eng.start_game()
    await eng.add_player("u1", "Alice")
    await eng.begin_presenting()
    await eng.begin_asking()

    # voice text → intent
    q_intent = classify_intent("他是侏儒嗎？")
    assert q_intent["intent"] == "question"

    s_intent = classify_intent("我投降")
    assert s_intent["intent"] == "surrender"

    f_intent = classify_intent("答案是他是侏儒")
    assert f_intent["intent"] == "final_answer"
    assert f_intent["payload"] == "他是侏儒"

    # 把 surrender 餵進 engine
    await eng.surrender("u1", "Alice")
    assert eng.session.state == TurtleSoupState.GAME_OVER
    assert eng.session.end_reason == EndReason.SURRENDER


@pytest.mark.asyncio
async def test_post_filter_blocks_leak_in_full_flow():
    """LLM 偶發洩底 → post_filter 阻擋 → narration 不含洩底詞。"""
    cb = AsyncMock()
    session = TurtleSoupSession(session_id=str(uuid.uuid4()), guild_id=1, channel_id=1)
    eng = TurtleSoupEngine(session=session, puzzle=ELEVATOR_18F, on_state_change=cb)
    await eng.start_game()
    await eng.add_player("u1", "Alice")
    await eng.begin_presenting()
    await eng.begin_asking()

    # Mock LLM 回一個含洩底詞的 narration
    from game.turtle_soup import llm_judge as judge_mod
    orig_cerebras = judge_mod._call_cerebras
    judge_mod._call_cerebras = AsyncMock(return_value={
        "verdict": "yes", "narration": "沒錯，他是侏儒",
    })
    try:
        # 玩家問題不含「侏儒」
        result = await eng.submit_question("u1", "Alice", "他害怕高處嗎？")
    finally:
        judge_mod._call_cerebras = orig_cerebras

    # post-filter 應已介入
    assert "侏儒" not in result["narration"]
    assert result["verdict"] == "yes"
