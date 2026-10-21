"""GroundedQAAgent v2 — 對談窗內非音樂追問一律交給 try_question_opener 判斷。

10/21：GroundedQAAgent 拔掉 _session_followup_bid/bid override（regex-only），
session 內的追問改走 IntentBus 沒人接時的 try_question_opener；sticky
（ctrl._wake_session_grounded_sticky）只決定 source 是 session_followup 還是
question_opener，不再自己出 Bid。WakeSession 的 sticky 判斷本體測試在
tests/test_wake_session.py。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import intent_agents.grounded_qa_agent as gqa
from intent_bus import IntentContext


def _ctx(query, speaker="A", mode="normal", dispatch_source="regex"):
    return IntentContext(
        speaker=speaker, raw_text=query, query=query, original_raw=query,
        wake_intent=0.9, stream_active=(mode == "stream"),
        game_mode=(mode == "game"), is_owner=False, now=0.0, mode=mode,
        dispatch_source=dispatch_source,
    )


def _ctrl(sticky: bool):
    ctrl = MagicMock()
    ctrl._wake_session_grounded_sticky = MagicMock(return_value=sticky)
    return ctrl


@pytest.mark.asyncio
async def test_sticky_true_calls_run_grounded_qa_with_session_followup_source():
    ctrl = _ctrl(True)
    with patch.object(gqa, "run_grounded_qa", new=AsyncMock(return_value="answered")) as mock_run:
        result = await gqa.try_question_opener(ctrl, _ctx("那他上一張專輯呢"))
    assert result is True
    mock_run.assert_awaited_once_with(
        ctrl, "A", "那他上一張專輯呢", raw="那他上一張專輯呢", source="session_followup")


@pytest.mark.asyncio
async def test_sticky_false_literal_calls_question_opener_source():
    ctrl = _ctrl(False)
    with patch.object(gqa, "run_grounded_qa", new=AsyncMock(return_value="answered")) as mock_run:
        await gqa.try_question_opener(ctrl, _ctx("那他上一張專輯呢"))
    mock_run.assert_awaited_once_with(
        ctrl, "A", "那他上一張專輯呢", raw="那他上一張專輯呢", source="question_opener")


@pytest.mark.asyncio
async def test_sticky_requires_literal_true_not_truthy_mock():
    """ctrl 是裸 MagicMock（_wake_session_grounded_sticky 回 truthy Mock，非 literal True）→ question_opener。"""
    ctrl = MagicMock()
    with patch.object(gqa, "run_grounded_qa", new=AsyncMock(return_value="answered")) as mock_run:
        await gqa.try_question_opener(ctrl, _ctx("那他上一張專輯呢"))
    mock_run.assert_awaited_once_with(
        ctrl, "A", "那他上一張專輯呢", raw="那他上一張專輯呢", source="question_opener")


@pytest.mark.asyncio
async def test_ctrl_without_sticky_attr_no_crash_falls_back_question_opener():
    ctrl = MagicMock(spec=[])
    with patch.object(gqa, "run_grounded_qa", new=AsyncMock(return_value="answered")) as mock_run:
        await gqa.try_question_opener(ctrl, _ctx("那他上一張專輯呢"))
    mock_run.assert_awaited_once_with(
        ctrl, "A", "那他上一張專輯呢", raw="那他上一張專輯呢", source="question_opener")


@pytest.mark.asyncio
async def test_game_mode_no_call():
    ctrl = _ctrl(True)
    with patch.object(gqa, "run_grounded_qa", new=AsyncMock()) as mock_run:
        result = await gqa.try_question_opener(ctrl, _ctx("那他上一張專輯呢", mode="game"))
    assert result is False
    mock_run.assert_not_awaited()


@pytest.mark.parametrize("query", ["播放晴天", "大聲一點", "嗯"])
@pytest.mark.asyncio
async def test_sticky_true_excluded_or_too_short_queries_no_call(query):
    ctrl = _ctrl(True)
    with patch.object(gqa, "run_grounded_qa", new=AsyncMock()) as mock_run:
        result = await gqa.try_question_opener(ctrl, _ctx(query))
    assert result is False
    mock_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_grounded_qa_notes_grounded_on_ctrl():
    ctrl = MagicMock()
    ctrl.active_text_channel.send = AsyncMock()
    ctrl._play_ack = AsyncMock()
    ctrl.play_tts = AsyncMock()
    ctrl.stt_logger = MagicMock()

    with patch.object(gqa, "grounded_answer", new=AsyncMock(return_value=None)), \
         patch.object(gqa, "build_recent_transcript_context", new=AsyncMock(return_value="")), \
         patch.object(gqa, "current_games", return_value=[]), \
         patch.object(gqa, "record_ambient_qa"):
        await gqa.run_grounded_qa(ctrl, "A", "x")

    ctrl._wake_session_note_grounded.assert_called_once_with("A")


# ── 口語點歌排除：沒 music agent 出價、_excluded 也不擋的點歌句不能被黏去 Google ──

@pytest.mark.parametrize("query", [
    "播晴天", "那就播這首", "聽聽看第一首", "好那播吧", "放五月天", "放第二首來聽", "來聽那首",
])
@pytest.mark.asyncio
async def test_sticky_spoken_play_requests_not_sent_to_grounded(query):
    ctrl = _ctrl(True)
    with patch.object(gqa, "run_grounded_qa", new=AsyncMock()) as mock_run:
        result = await gqa.try_question_opener(ctrl, _ctx(query))
    assert result is False
    mock_run.assert_not_awaited()


@pytest.mark.parametrize("query", [
    "那他上一張專輯呢", "他放了幾張專輯", "那首歌哪一年發的", "放假那天發的嗎",
])
@pytest.mark.asyncio
async def test_sticky_info_followups_with_play_like_chars_still_grounded(query):
    ctrl = _ctrl(True)
    with patch.object(gqa, "run_grounded_qa", new=AsyncMock(return_value="answered")) as mock_run:
        result = await gqa.try_question_opener(ctrl, _ctx(query))
    assert result is True
    mock_run.assert_awaited_once_with(ctrl, "A", query, raw=query, source="session_followup")


# ── B：session_followup 查詢結果要回報 WakeSession（2026-10-15 事故：
# 追問黏著連續查不到卻一直被 on_replied 救回，session 永遠不結束）──────────

@pytest.mark.asyncio
async def test_run_grounded_qa_reports_miss_on_session_followup_no_answer():
    ctrl = MagicMock()
    ctrl.active_text_channel.send = AsyncMock()
    ctrl._play_ack = AsyncMock()
    ctrl.play_tts = AsyncMock()
    ctrl.stt_logger = MagicMock()

    with patch.object(gqa, "grounded_answer", new=AsyncMock(return_value=None)), \
         patch.object(gqa, "build_recent_transcript_context", new=AsyncMock(return_value="")), \
         patch.object(gqa, "current_games", return_value=[]), \
         patch.object(gqa, "record_ambient_qa"):
        await gqa.run_grounded_qa(ctrl, "A", "x", source="session_followup")

    ctrl._wake_session_on_followup_result.assert_called_once_with("A", False)


@pytest.mark.asyncio
async def test_run_grounded_qa_reports_hit_on_session_followup_answer():
    ctrl = MagicMock()
    ctrl.active_text_channel.send = AsyncMock()
    ctrl._play_ack = AsyncMock()
    ctrl.play_tts = AsyncMock()
    ctrl.stt_logger = MagicMock()

    with patch.object(gqa, "grounded_answer", new=AsyncMock(return_value=("答", ["src"]))), \
         patch.object(gqa, "build_recent_transcript_context", new=AsyncMock(return_value="")), \
         patch.object(gqa, "current_games", return_value=[]), \
         patch.object(gqa, "record_ambient_qa"):
        await gqa.run_grounded_qa(ctrl, "A", "x", source="session_followup")

    ctrl._wake_session_on_followup_result.assert_called_once_with("A", True)


@pytest.mark.asyncio
async def test_run_grounded_qa_reports_miss_for_ambient_qa_source():
    """10/17 事故修正：regex 路徑（ambient_qa）查不到也要計數，否則 session 停不下來。"""
    ctrl = MagicMock()
    ctrl.active_text_channel.send = AsyncMock()
    ctrl._play_ack = AsyncMock()
    ctrl.play_tts = AsyncMock()
    ctrl.stt_logger = MagicMock()

    with patch.object(gqa, "grounded_answer", new=AsyncMock(return_value=None)), \
         patch.object(gqa, "build_recent_transcript_context", new=AsyncMock(return_value="")), \
         patch.object(gqa, "current_games", return_value=[]), \
         patch.object(gqa, "record_ambient_qa"):
        await gqa.run_grounded_qa(ctrl, "A", "x", source="ambient_qa")

    ctrl._wake_session_on_followup_result.assert_called_once_with("A", False)


# ── E：多人在場回覆第一句叫名字（2026-10-17）──────────────────────────────

@pytest.mark.asyncio
async def test_run_grounded_qa_no_answer_prefixes_address():
    ctrl = MagicMock()
    ctrl.active_text_channel.send = AsyncMock()
    ctrl._play_ack = AsyncMock()
    ctrl.play_tts = AsyncMock()
    ctrl.stt_logger = MagicMock()
    ctrl._reply_address_prefix = MagicMock(return_value="showay，")

    with patch.object(gqa, "grounded_answer", new=AsyncMock(return_value=None)), \
         patch.object(gqa, "build_recent_transcript_context", new=AsyncMock(return_value="")), \
         patch.object(gqa, "current_games", return_value=[]), \
         patch.object(gqa, "record_ambient_qa"):
        await gqa.run_grounded_qa(ctrl, "A", "x")

    assert ctrl.play_tts.call_args.args[0] == "showay，這題我查不到。"


@pytest.mark.asyncio
async def test_run_grounded_qa_answer_prefixes_address_but_not_text_channel():
    ctrl = MagicMock()
    ctrl.active_text_channel.send = AsyncMock()
    ctrl._play_ack = AsyncMock()
    ctrl.play_tts = AsyncMock()
    ctrl.stt_logger = MagicMock()
    ctrl._reply_address_prefix = MagicMock(return_value="showay，")

    with patch.object(gqa, "grounded_answer", new=AsyncMock(return_value=("答", ["src"]))), \
         patch.object(gqa, "build_recent_transcript_context", new=AsyncMock(return_value="")), \
         patch.object(gqa, "current_games", return_value=[]), \
         patch.object(gqa, "record_ambient_qa"):
        await gqa.run_grounded_qa(ctrl, "A", "x")

    assert ctrl.play_tts.call_args.args[0] == "showay，答"
    _sent = ctrl.active_text_channel.send.call_args.args[0]
    assert "showay，答" not in _sent


@pytest.mark.asyncio
async def test_run_grounded_qa_plain_mock_ctrl_no_prefix():
    """ctrl 是普通 MagicMock（沒設 _reply_address_prefix）→ 不加前綴。"""
    ctrl = MagicMock()
    ctrl.active_text_channel.send = AsyncMock()
    ctrl._play_ack = AsyncMock()
    ctrl.play_tts = AsyncMock()
    ctrl.stt_logger = MagicMock()

    with patch.object(gqa, "grounded_answer", new=AsyncMock(return_value=None)), \
         patch.object(gqa, "build_recent_transcript_context", new=AsyncMock(return_value="")), \
         patch.object(gqa, "current_games", return_value=[]), \
         patch.object(gqa, "record_ambient_qa"):
        await gqa.run_grounded_qa(ctrl, "A", "x")

    assert ctrl.play_tts.call_args.args[0] == "這題我查不到。"
