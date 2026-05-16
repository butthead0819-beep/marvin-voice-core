"""
TDD tests for mark-done voice intent in RecallHandler.

Flow: 「那件事做完了」→ 找出對應 task → update_status(done)

Run with:
    pytest tests/test_mark_done.py -v
"""
from __future__ import annotations

import time
import pytest
from unittest.mock import AsyncMock, MagicMock, call


# ═══════════════════════════════════════════════════════
# is_mark_done_query 純函數
# ═══════════════════════════════════════════════════════

def test_mark_done_detects_done():
    from recall_handler import is_mark_done_query
    assert is_mark_done_query("那件事做完了") is True


def test_mark_done_detects_finished():
    from recall_handler import is_mark_done_query
    assert is_mark_done_query("已經完成了") is True


def test_mark_done_detects_sent():
    from recall_handler import is_mark_done_query
    assert is_mark_done_query("信寄出去了") is True


def test_mark_done_detects_checked():
    from recall_handler import is_mark_done_query
    assert is_mark_done_query("API 文件查好了") is True


def test_mark_done_does_not_trigger_on_recall():
    from recall_handler import is_mark_done_query
    assert is_mark_done_query("我剛才說了什麼") is False
    assert is_mark_done_query("我今天還有什麼待辦") is False
    assert is_mark_done_query("幫我放一首歌") is False


# ═══════════════════════════════════════════════════════
# handle_mark_done pipeline 測試
# ═══════════════════════════════════════════════════════

def _task(id, text, direction="inbound"):
    return {
        "id": id, "text": text, "direction": direction, "assignee": "狗與鹿",
        "status": "pending", "due_date": None,
        "source_quote": f"我說要{text}",
        "source_window_start": time.time() - 300,
        "source_window_end": time.time(),
        "created_at": time.time(),
    }


def _make_handler(tasks):
    from recall_handler import RecallHandler
    task_store = MagicMock()
    task_store.get_pending.return_value = tasks
    task_store.search.return_value = tasks
    task_store.update_status = MagicMock()

    return RecallHandler(
        summary_store=MagicMock(**{"search.return_value": [], "get_summaries.return_value": []}),
        task_store=task_store,
        transcript_store=MagicMock(**{"get_recent.return_value": []}),
        groq_client=MagicMock(**{"chat.completions.create": AsyncMock(return_value=MagicMock(**{"choices[0].message.content": "好的"}))}),
        guild_id=1,
        owner_speaker="狗與鹿",
    )


@pytest.mark.asyncio
async def test_mark_done_single_task_marks_it():
    tasks = [_task(id=1, text="查 API 文件")]
    handler = _make_handler(tasks)

    result = await handler.handle_mark_done(speaker="狗與鹿", query="API 文件查好了")

    handler.task_store.update_status.assert_called_once_with(1, "done")
    assert "完成" in result or "API" in result or "做完" in result


@pytest.mark.asyncio
async def test_mark_done_matches_by_keyword():
    tasks = [
        _task(id=1, text="查 API 文件"),
        _task(id=2, text="買咖啡豆"),
    ]
    handler = _make_handler(tasks)
    # keyword "咖啡" 應該命中 id=2
    handler.task_store.search.return_value = [_task(id=2, text="買咖啡豆")]

    result = await handler.handle_mark_done(speaker="狗與鹿", query="咖啡豆買好了")

    handler.task_store.update_status.assert_called_once_with(2, "done")


@pytest.mark.asyncio
async def test_mark_done_no_tasks_returns_friendly_message():
    handler = _make_handler(tasks=[])

    result = await handler.handle_mark_done(speaker="狗與鹿", query="那件事做完了")

    handler.task_store.update_status.assert_not_called()
    assert result  # 不 crash，給友善訊息
    assert "沒有" in result or "找不到" in result or "空" in result


@pytest.mark.asyncio
async def test_mark_done_ambiguous_multiple_tasks_asks_which():
    tasks = [
        _task(id=1, text="查 API 文件"),
        _task(id=2, text="買咖啡豆"),
        _task(id=3, text="寄信給客戶"),
    ]
    handler = _make_handler(tasks)
    # search 回傳空（沒有精確 keyword 命中），所以 3 個都是候選
    handler.task_store.search.return_value = []

    result = await handler.handle_mark_done(speaker="狗與鹿", query="那件事做完了")

    handler.task_store.update_status.assert_not_called()
    # 應該回傳「哪一件」的提示
    assert "哪" in result or "件" in result or "哪一個" in result or "列" in result


@pytest.mark.asyncio
async def test_mark_done_cancelled_also_works():
    tasks = [_task(id=5, text="整理行程")]
    handler = _make_handler(tasks)

    result = await handler.handle_mark_done(
        speaker="狗與鹿", query="整理行程不用做了", status="cancelled"
    )

    handler.task_store.update_status.assert_called_once_with(5, "cancelled")
