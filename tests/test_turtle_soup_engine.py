"""TDD — 海龜湯 TurtleSoupEngine

覆蓋 state machine 所有轉移：
- IDLE → JOINING（start_game）
- JOINING → PRESENTING（後續邏輯由 cog 推進）
- PRESENTING → ASKING
- ASKING ⇄ ASKING（submit_question 留在原 state）
- ASKING → GAME_OVER（投降 / 最終猜對 / 題數用完）
- 各種防呆：非 ASKING 狀態 submit_question 應被拒絕
"""
from __future__ import annotations
import uuid
import pytest
from unittest.mock import AsyncMock, patch

from game.turtle_soup.session import (
    TurtleSoupSession, TurtleSoupState, EndReason,
)
from game.turtle_soup.puzzles import ELEVATOR_18F


def _new_session():
    return TurtleSoupSession(
        session_id=str(uuid.uuid4()), guild_id=1, channel_id=1,
        puzzle_id=ELEVATOR_18F.id,
    )


def _stub_callback():
    return AsyncMock()


# ── add_player / start_game / advance_to_presenting / advance_to_asking ──────

@pytest.mark.asyncio
async def test_initial_state_is_idle():
    from game.turtle_soup.engine import TurtleSoupEngine
    eng = TurtleSoupEngine(session=_new_session(), puzzle=ELEVATOR_18F, on_state_change=_stub_callback())
    assert eng.session.state == TurtleSoupState.IDLE


@pytest.mark.asyncio
async def test_start_game_moves_to_joining_and_fires_callback():
    from game.turtle_soup.engine import TurtleSoupEngine
    cb = _stub_callback()
    eng = TurtleSoupEngine(session=_new_session(), puzzle=ELEVATOR_18F, on_state_change=cb)
    await eng.start_game()
    assert eng.session.state == TurtleSoupState.JOINING
    cb.assert_called_once()


@pytest.mark.asyncio
async def test_add_player_appends_and_dedups():
    from game.turtle_soup.engine import TurtleSoupEngine
    eng = TurtleSoupEngine(session=_new_session(), puzzle=ELEVATOR_18F, on_state_change=_stub_callback())
    await eng.start_game()
    assert await eng.add_player("u1", "Alice") is True
    assert await eng.add_player("u1", "Alice") is False  # 重複加入
    assert len(eng.session.players) == 1


@pytest.mark.asyncio
async def test_begin_presenting_requires_joining_state():
    from game.turtle_soup.engine import TurtleSoupEngine
    eng = TurtleSoupEngine(session=_new_session(), puzzle=ELEVATOR_18F, on_state_change=_stub_callback())
    # IDLE 狀態 begin_presenting 應被拒
    result = await eng.begin_presenting()
    assert result is False
    assert eng.session.state == TurtleSoupState.IDLE


@pytest.mark.asyncio
async def test_full_phase_progression():
    from game.turtle_soup.engine import TurtleSoupEngine
    eng = TurtleSoupEngine(session=_new_session(), puzzle=ELEVATOR_18F, on_state_change=_stub_callback())
    await eng.start_game()
    await eng.add_player("u1", "Alice")
    await eng.begin_presenting()
    assert eng.session.state == TurtleSoupState.PRESENTING
    await eng.begin_asking()
    assert eng.session.state == TurtleSoupState.ASKING


# ── submit_question ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_question_only_works_in_asking_state():
    from game.turtle_soup.engine import TurtleSoupEngine
    eng = TurtleSoupEngine(session=_new_session(), puzzle=ELEVATOR_18F, on_state_change=_stub_callback())
    await eng.start_game()
    # JOINING state submit → reject
    result = await eng.submit_question("u1", "Alice", "他是侏儒嗎？")
    assert result is None


@pytest.mark.asyncio
async def test_submit_question_calls_judge_and_appends_history():
    from game.turtle_soup.engine import TurtleSoupEngine
    from game.turtle_soup import llm_judge

    eng = TurtleSoupEngine(session=_new_session(), puzzle=ELEVATOR_18F, on_state_change=_stub_callback())
    await eng.start_game()
    await eng.add_player("u1", "Alice")
    await eng.begin_presenting()
    await eng.begin_asking()

    with patch.object(llm_judge, "judge_question", new=AsyncMock(return_value={
        "verdict": "yes",
        "narration": "你抓到了",
        "_provider": "Cerebras",
    })):
        result = await eng.submit_question("u1", "Alice", "他是侏儒嗎？")

    assert result["verdict"] == "yes"
    assert result["narration"] == "你抓到了"
    assert eng.session.questions_count == 1
    assert eng.session.asked_questions[0].question == "他是侏儒嗎？"
    assert eng.session.asked_questions[0].asker_name == "Alice"


@pytest.mark.asyncio
async def test_submit_question_exhaustion_triggers_game_over():
    """達 max_questions 後再問 → 直接結束（exhausted）。"""
    from game.turtle_soup.engine import TurtleSoupEngine
    from game.turtle_soup import llm_judge

    session = _new_session()
    session.max_questions = 3  # 把上限調低方便測
    cb = _stub_callback()
    eng = TurtleSoupEngine(session=session, puzzle=ELEVATOR_18F, on_state_change=cb)
    await eng.start_game()
    await eng.add_player("u1", "Alice")
    await eng.begin_presenting()
    await eng.begin_asking()

    with patch.object(llm_judge, "judge_question", new=AsyncMock(return_value={
        "verdict": "no", "narration": "想太多", "_provider": "Cerebras",
    })):
        for i in range(3):
            await eng.submit_question("u1", "Alice", f"q{i}")

    assert eng.session.state == TurtleSoupState.GAME_OVER
    assert eng.session.end_reason == EndReason.EXHAUSTED


# ── surrender ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_surrender_in_asking_ends_game():
    from game.turtle_soup.engine import TurtleSoupEngine
    eng = TurtleSoupEngine(session=_new_session(), puzzle=ELEVATOR_18F, on_state_change=_stub_callback())
    await eng.start_game()
    await eng.add_player("u1", "Alice")
    await eng.begin_presenting()
    await eng.begin_asking()

    await eng.surrender("u1", "Alice")
    assert eng.session.state == TurtleSoupState.GAME_OVER
    assert eng.session.end_reason == EndReason.SURRENDER


@pytest.mark.asyncio
async def test_surrender_outside_asking_is_noop():
    from game.turtle_soup.engine import TurtleSoupEngine
    eng = TurtleSoupEngine(session=_new_session(), puzzle=ELEVATOR_18F, on_state_change=_stub_callback())
    await eng.start_game()
    # JOINING 投降 → 無動作
    await eng.surrender("u1", "Alice")
    assert eng.session.state == TurtleSoupState.JOINING


# ── submit_final_guess ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_correct_final_guess_wins():
    from game.turtle_soup.engine import TurtleSoupEngine
    from game.turtle_soup import llm_judge

    eng = TurtleSoupEngine(session=_new_session(), puzzle=ELEVATOR_18F, on_state_change=_stub_callback())
    await eng.start_game()
    await eng.add_player("u1", "Alice")
    await eng.begin_presenting()
    await eng.begin_asking()

    with patch.object(llm_judge, "judge_final_guess", new=AsyncMock(return_value={
        "accepted": True,
        "covered_facts": [0, 1, 2],
        "narration": "想通了！",
        "_provider": "Cerebras",
    })):
        result = await eng.submit_final_guess("u1", "Alice", "他是侏儒按不到 22 樓按鈕")

    assert result["accepted"] is True
    assert eng.session.state == TurtleSoupState.GAME_OVER
    assert eng.session.end_reason == EndReason.WIN


@pytest.mark.asyncio
async def test_incorrect_final_guess_returns_to_asking():
    """猜錯不結束遊戲，保留 ASKING 狀態。"""
    from game.turtle_soup.engine import TurtleSoupEngine
    from game.turtle_soup import llm_judge

    eng = TurtleSoupEngine(session=_new_session(), puzzle=ELEVATOR_18F, on_state_change=_stub_callback())
    await eng.start_game()
    await eng.add_player("u1", "Alice")
    await eng.begin_presenting()
    await eng.begin_asking()

    with patch.object(llm_judge, "judge_final_guess", new=AsyncMock(return_value={
        "accepted": False,
        "covered_facts": [0],
        "narration": "差一點",
        "_provider": "Cerebras",
    })):
        result = await eng.submit_final_guess("u1", "Alice", "他是侏儒")

    assert result["accepted"] is False
    assert eng.session.state == TurtleSoupState.ASKING  # 未結束


# ── cancel ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_from_any_state_ends_with_cancelled_reason():
    from game.turtle_soup.engine import TurtleSoupEngine
    eng = TurtleSoupEngine(session=_new_session(), puzzle=ELEVATOR_18F, on_state_change=_stub_callback())
    await eng.start_game()
    await eng.cancel()
    assert eng.session.state == TurtleSoupState.GAME_OVER
    assert eng.session.end_reason == EndReason.CANCELLED


# ── on_state_change 觸發 ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_state_change_callback_fired_on_every_transition():
    from game.turtle_soup.engine import TurtleSoupEngine
    cb = _stub_callback()
    eng = TurtleSoupEngine(session=_new_session(), puzzle=ELEVATOR_18F, on_state_change=cb)
    await eng.start_game()         # IDLE → JOINING
    await eng.add_player("u1", "Alice")
    await eng.begin_presenting()   # JOINING → PRESENTING
    await eng.begin_asking()       # PRESENTING → ASKING
    await eng.surrender("u1", "Alice")  # ASKING → GAME_OVER
    assert cb.call_count == 4
