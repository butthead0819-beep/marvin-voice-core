"""TDD — turtle_soup_cog state dispatch + STT hook + SFX mapping。

Cog 是 Discord glue layer。測試重點：
- on_state_change 對應每個 state 觸發正確動作（用 mock 攔截）
- receive_voice_answer_by_speaker 正確路由意圖
- inflight cap 過載保護
- VERDICT_SFX 對應表
"""
from __future__ import annotations
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock

from game.turtle_soup.session import (
    EndReason,
    TurtleSoupSession,
    TurtleSoupState,
)
from game.turtle_soup.puzzles import ELEVATOR_18F


def _make_bot():
    bot = MagicMock()
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.engine = MagicMock()
    return bot


def _make_session(state=TurtleSoupState.IDLE):
    s = TurtleSoupSession(
        session_id=str(uuid.uuid4()), guild_id=1, channel_id=1,
        puzzle_id=ELEVATOR_18F.id,
    )
    s.state = state
    return s


def _make_cog():
    from cogs.turtle_soup_cog import TurtleSoupCog
    bot = _make_bot()
    cog = TurtleSoupCog(bot)
    cog._post_or_edit = AsyncMock()
    cog._fire_tts = AsyncMock()
    cog._play_sfx = AsyncMock()
    cog._enter_game_mode = MagicMock()
    cog._exit_game_mode = MagicMock()
    cog._channel = AsyncMock()
    cog._channel.send = AsyncMock()
    return cog


# ── VERDICT_SFX mapping ──────────────────────────────────────────────────────

def test_verdict_sfx_table_covers_three_verdicts():
    from cogs.turtle_soup_cog import VERDICT_SFX
    assert VERDICT_SFX["yes"] == "correct"
    assert VERDICT_SFX["no"] == "buzz"
    assert VERDICT_SFX["irrelevant"] == "ba_dum_tss"


# ── is_active ────────────────────────────────────────────────────────────────

def test_is_active_false_when_no_session():
    cog = _make_cog()
    assert cog.is_active() is False


@pytest.mark.parametrize("state,expected", [
    (TurtleSoupState.IDLE, False),
    (TurtleSoupState.JOINING, True),
    (TurtleSoupState.PRESENTING, True),
    (TurtleSoupState.ASKING, True),
    (TurtleSoupState.GAME_OVER, False),
])
def test_is_active_by_state(state, expected):
    cog = _make_cog()
    cog._session = _make_session(state)
    cog._engine = MagicMock()
    assert cog.is_active() is expected


# ── should_suppress_for_game_by_id ──────────────────────────────────────────

def test_suppress_false_when_no_session():
    cog = _make_cog()
    assert cog.should_suppress_for_game_by_id(12345) is False


def test_suppress_false_in_idle_or_game_over():
    cog = _make_cog()
    for state in (TurtleSoupState.IDLE, TurtleSoupState.GAME_OVER):
        cog._session = _make_session(state)
        assert cog.should_suppress_for_game_by_id(12345) is False


def test_suppress_false_in_joining_state():
    """JOINING 階段不過濾，玩家可能正在按 Join。"""
    cog = _make_cog()
    cog._session = _make_session(TurtleSoupState.JOINING)
    assert cog.should_suppress_for_game_by_id(12345) is False


def test_suppress_true_in_presenting():
    """PRESENTING 時 Marvin 念湯面，全部過濾。"""
    cog = _make_cog()
    cog._session = _make_session(TurtleSoupState.PRESENTING)
    assert cog.should_suppress_for_game_by_id(12345) is True


def test_suppress_true_in_asking_for_non_participant():
    """ASKING 階段：非 session.players 的人語音丟棄。"""
    from game.turtle_soup.session import TurtleSoupPlayer
    cog = _make_cog()
    cog._session = _make_session(TurtleSoupState.ASKING)
    cog._session.players = [TurtleSoupPlayer(user_id="11111", display_name="Alice")]
    assert cog.should_suppress_for_game_by_id(99999) is True  # 非玩家
    assert cog.should_suppress_for_game_by_id(11111) is False  # 玩家


# ── on_state_change dispatch ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_on_state_change_joining_posts_embed_and_enters_game_mode():
    cog = _make_cog()
    cog._engine = MagicMock()
    cog._engine.puzzle = ELEVATOR_18F
    session = _make_session(TurtleSoupState.JOINING)
    await cog.on_state_change(session)

    cog._post_or_edit.assert_called_once()
    cog._enter_game_mode.assert_called_once()


@pytest.mark.asyncio
async def test_on_state_change_asking_posts_embed():
    cog = _make_cog()
    cog._engine = MagicMock()
    cog._engine.puzzle = ELEVATOR_18F
    session = _make_session(TurtleSoupState.ASKING)
    await cog.on_state_change(session)

    cog._post_or_edit.assert_called_once()


@pytest.mark.asyncio
async def test_on_state_change_game_over_posts_embed_and_exits_game_mode_eventually():
    """GAME_OVER 會 spawn announce_truth_and_cleanup，內部會 _exit_game_mode。"""
    import asyncio
    cog = _make_cog()
    cog._engine = MagicMock()
    cog._engine.puzzle = ELEVATOR_18F
    session = _make_session(TurtleSoupState.GAME_OVER)
    session.end_reason = EndReason.SURRENDER

    # 攔截 sleep 讓 cleanup 立刻完成
    cog._announce_truth_and_cleanup = AsyncMock()

    await cog.on_state_change(session)
    # 給 spawn task 跑
    for _ in range(3):
        await asyncio.sleep(0)

    cog._post_or_edit.assert_called_once()
    cog._announce_truth_and_cleanup.assert_called_once()


# ── receive_voice_answer_by_speaker ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_voice_routes_ignored_when_no_engine():
    cog = _make_cog()
    assert await cog.receive_voice_answer_by_speaker("Alice", "他是侏儒嗎？") is False


@pytest.mark.asyncio
async def test_voice_routes_ignored_when_state_not_asking():
    cog = _make_cog()
    cog._engine = MagicMock()
    cog._session = _make_session(TurtleSoupState.JOINING)
    assert await cog.receive_voice_answer_by_speaker("Alice", "他是侏儒嗎？") is False


@pytest.mark.asyncio
async def test_voice_filler_word_ignored():
    cog = _make_cog()
    cog._engine = AsyncMock()
    cog._session = _make_session(TurtleSoupState.ASKING)
    assert await cog.receive_voice_answer_by_speaker("Alice", "嗯") is False
    cog._engine.submit_question.assert_not_called()


@pytest.mark.asyncio
async def test_voice_surrender_intent_calls_engine_surrender():
    cog = _make_cog()
    cog._engine = AsyncMock()
    cog._session = _make_session(TurtleSoupState.ASKING)
    assert await cog.receive_voice_answer_by_speaker("Alice", "我投降") is True
    cog._engine.surrender.assert_called_once()


@pytest.mark.asyncio
async def test_voice_question_intent_dispatches_to_handle_question():
    """玩家用「請問」開頭 → question 意圖 → spawn handle_question。"""
    import asyncio
    cog = _make_cog()
    cog._engine = AsyncMock()
    cog._engine.submit_question = AsyncMock(return_value={
        "verdict": "yes", "narration": "沒錯", "_provider": "Cerebras",
    })
    cog._session = _make_session(TurtleSoupState.ASKING)
    cog._build_asking_embed = MagicMock(return_value=None)

    ok = await cog.receive_voice_answer_by_speaker("Alice", "請問他是侏儒嗎？")
    assert ok is True
    # 等 spawn task 完成
    for _ in range(5):
        await asyncio.sleep(0)

    cog._engine.submit_question.assert_called_once()


@pytest.mark.asyncio
async def test_voice_discussion_intent_ignored_silently():
    """沒有「請問」前綴的句子 → discussion → 靜默忽略，不送 LLM 不播 SFX/TTS。"""
    cog = _make_cog()
    cog._engine = AsyncMock()
    cog._session = _make_session(TurtleSoupState.ASKING)

    # 像問句但沒前綴 → discussion
    ok = await cog.receive_voice_answer_by_speaker("Alice", "他是侏儒嗎？")
    assert ok is False
    cog._engine.submit_question.assert_not_called()

    # 玩家內部討論
    ok = await cog.receive_voice_answer_by_speaker("Bob", "我覺得他怕高")
    assert ok is False
    cog._engine.submit_question.assert_not_called()


@pytest.mark.asyncio
async def test_voice_question_inflight_cap_drops_when_full():
    cog = _make_cog()
    cog._engine = AsyncMock()
    cog._session = _make_session(TurtleSoupState.ASKING)
    cog._asking_inflight = cog._MAX_ASKING_INFLIGHT  # 已滿

    ok = await cog.receive_voice_answer_by_speaker("Alice", "請問他害怕電梯嗎？")
    assert ok is False
    cog._channel.send.assert_called_once()
    cog._engine.submit_question.assert_not_called()


@pytest.mark.asyncio
async def test_voice_final_answer_dispatches_to_handle_final_guess():
    import asyncio
    cog = _make_cog()
    cog._engine = AsyncMock()
    cog._engine.submit_final_guess = AsyncMock(return_value={
        "accepted": True, "covered_facts": [0, 1], "narration": "答對了",
        "_provider": "Cerebras",
    })
    cog._session = _make_session(TurtleSoupState.ASKING)

    ok = await cog.receive_voice_answer_by_speaker("Alice", "答案是他是侏儒")
    assert ok is True
    for _ in range(5):
        await asyncio.sleep(0)

    cog._engine.submit_final_guess.assert_called_once()
    args = cog._engine.submit_final_guess.call_args
    # payload 應已去除「答案是」prefix
    assert "他是侏儒" in args[0][2]


# ── _fire_verdict_sequence SFX/TTS 序列 ──────────────────────────────────────

@pytest.mark.asyncio
async def test_fire_verdict_sequence_plays_sfx_then_tts_in_order():
    import asyncio
    cog = _make_cog()
    cog._engine = MagicMock()
    cog._session = _make_session(TurtleSoupState.ASKING)

    vc_mock = AsyncMock()
    cog.bot.cogs.get.side_effect = lambda name: vc_mock if name == "VoiceController" else None

    events: list[str] = []
    cog._play_sfx = AsyncMock(side_effect=lambda n: events.append(f"SFX:{n}"))
    cog._fire_tts = AsyncMock(side_effect=lambda v, t: events.append(f"TTS:{t}"))

    await cog._fire_verdict_sequence("yes", "你抓到了")
    for _ in range(5):
        await asyncio.sleep(0)

    assert events == ["SFX:correct", "TTS:你抓到了"]


@pytest.mark.asyncio
async def test_fire_verdict_sequence_uses_correct_sfx_per_verdict():
    import asyncio
    cog = _make_cog()
    vc_mock = AsyncMock()
    cog.bot.cogs.get.side_effect = lambda name: vc_mock if name == "VoiceController" else None

    for verdict, expected_sfx in (
        ("yes", "correct"),
        ("no", "buzz"),
        ("irrelevant", "ba_dum_tss"),
    ):
        captured = []
        cog._play_sfx = AsyncMock(side_effect=lambda n, c=captured: c.append(n))
        cog._fire_tts = AsyncMock()
        await cog._fire_verdict_sequence(verdict, "test")
        for _ in range(5):
            await asyncio.sleep(0)
        assert captured == [expected_sfx]


# ── hint_request intent + idle timer ────────────────────────────────────────

@pytest.mark.asyncio
async def test_voice_hint_request_intent_dispatches_handler():
    import asyncio
    cog = _make_cog()
    cog._engine = AsyncMock()
    cog._engine.request_hint = AsyncMock(return_value="想想他的身體特徵。")
    cog._session = _make_session(TurtleSoupState.ASKING)
    cog._session.hints_given = 1

    vc_mock = AsyncMock()
    cog.bot.cogs.get.side_effect = lambda name: vc_mock if name == "VoiceController" else None

    ok = await cog.receive_voice_answer_by_speaker("Alice", "請問可以給我提示嗎")
    assert ok is True
    for _ in range(5):
        await asyncio.sleep(0)

    cog._engine.request_hint.assert_called_once()


@pytest.mark.asyncio
async def test_hint_request_handler_plays_sfx_tts_when_hint_returned():
    import asyncio
    cog = _make_cog()
    cog._engine = AsyncMock()
    cog._engine.request_hint = AsyncMock(return_value="想想他的身體特徵。")
    cog._session = _make_session(TurtleSoupState.ASKING)
    cog._session.hints_given = 1  # 模擬已 +1 後

    vc_mock = AsyncMock()
    cog.bot.cogs.get.side_effect = lambda name: vc_mock if name == "VoiceController" else None

    events = []
    cog._play_sfx = AsyncMock(side_effect=lambda n: events.append(f"SFX:{n}"))
    cog._fire_tts = AsyncMock(side_effect=lambda v, t: events.append(f"TTS:{t}"))

    await cog._handle_hint_request(source="player")
    for _ in range(5):
        await asyncio.sleep(0)

    assert events[0] == "SFX:fanfare"
    assert any("提示" in e for e in events)
    cog._channel.send.assert_called_once()


@pytest.mark.asyncio
async def test_hint_request_exhausted_player_gets_response():
    """玩家要 hint 但已用完 → 回「給完」訊息。"""
    cog = _make_cog()
    cog._engine = AsyncMock()
    cog._engine.request_hint = AsyncMock(return_value=None)
    cog._session = _make_session(TurtleSoupState.ASKING)
    cog._fire_verdict_sequence = AsyncMock()

    await cog._handle_hint_request(source="player")
    cog._fire_verdict_sequence.assert_called_once()
    args, _ = cog._fire_verdict_sequence.call_args
    assert "給完" in args[1] or "完" in args[1]


@pytest.mark.asyncio
async def test_hint_request_exhausted_idle_silent():
    """idle timer 觸發但 hint 已用完 → 靜默，不再打擾。"""
    cog = _make_cog()
    cog._engine = AsyncMock()
    cog._engine.request_hint = AsyncMock(return_value=None)
    cog._session = _make_session(TurtleSoupState.ASKING)
    cog._fire_verdict_sequence = AsyncMock()

    await cog._handle_hint_request(source="idle")
    cog._fire_verdict_sequence.assert_not_called()
    cog._channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_idle_timer_fires_auto_hint_after_interval():
    """idle timer 跑完 → 自動呼叫 _handle_hint_request。"""
    import asyncio
    cog = _make_cog()
    cog._engine = MagicMock()
    cog._session = _make_session(TurtleSoupState.ASKING)
    cog._handle_hint_request = AsyncMock()
    cog._IDLE_HINT_INTERVAL = 0.01  # 加速測試

    cog._start_idle_hint_timer()
    await asyncio.sleep(0.05)

    cog._handle_hint_request.assert_called_once_with(source="idle")


@pytest.mark.asyncio
async def test_idle_timer_cancelled_on_new_question():
    """玩家問問題 → idle timer 應被取消重啟。"""
    import asyncio
    cog = _make_cog()
    cog._engine = AsyncMock()
    cog._session = _make_session(TurtleSoupState.ASKING)
    cog._handle_hint_request = AsyncMock()
    cog._IDLE_HINT_INTERVAL = 0.1

    cog._start_idle_hint_timer()
    first_task = cog._idle_hint_task
    # 玩家問問題之前 timer 已啟動
    assert first_task is not None
    assert not first_task.done()

    # 模擬玩家問題重啟 timer
    cog._start_idle_hint_timer()
    # 第一個 task 應被取消
    await asyncio.sleep(0)
    assert first_task.cancelled() or first_task.done()


@pytest.mark.asyncio
async def test_idle_timer_no_op_outside_asking_state():
    cog = _make_cog()
    cog._engine = MagicMock()
    cog._session = _make_session(TurtleSoupState.JOINING)
    cog._start_idle_hint_timer()
    assert cog._idle_hint_task is None
