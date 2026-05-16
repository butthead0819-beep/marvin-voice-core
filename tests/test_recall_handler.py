"""
TDD tests for RecallHandler — voice-native task/diary recall pipeline.

Run with:
    pytest tests/test_recall_handler.py -v
"""
from __future__ import annotations

import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock


# ═══════════════════════════════════════════════════════
# is_recall_query 純函數測試
# ═══════════════════════════════════════════════════════

def test_recall_query_detects_what_did_i_say():
    from recall_handler import is_recall_query
    assert is_recall_query("我剛才說了什麼") is True


def test_recall_query_detects_commitment():
    from recall_handler import is_recall_query
    assert is_recall_query("我有沒有答應過什麼") is True


def test_recall_query_detects_todo():
    from recall_handler import is_recall_query
    assert is_recall_query("我今天還有什麼待辦") is True


def test_recall_query_detects_delegated():
    from recall_handler import is_recall_query
    assert is_recall_query("我交辦給 showay 的那件事") is True


def test_recall_query_detects_earlier_topic():
    from recall_handler import is_recall_query
    assert is_recall_query("我早上提到的那個事情") is True


def test_recall_query_does_not_trigger_on_normal_chat():
    from recall_handler import is_recall_query
    assert is_recall_query("今天天氣怎麼樣") is False
    assert is_recall_query("幫我放一首歌") is False
    assert is_recall_query("系統狀態如何") is False


# ═══════════════════════════════════════════════════════
# RecallHandler pipeline 測試
# ═══════════════════════════════════════════════════════

def _make_summary_store(summaries: list[dict]):
    store = MagicMock()
    store.search.return_value = summaries
    store.get_summaries.return_value = summaries
    return store


def _make_task_store(tasks: list[dict]):
    store = MagicMock()
    store.get_pending.return_value = tasks
    store.search.return_value = tasks
    return store


def _make_transcript_store(utterances: list[dict]):
    store = MagicMock()
    store.get_recent.return_value = utterances
    return store


def _make_groq_response(text: str):
    resp = MagicMock()
    resp.choices[0].message.content = text
    return resp


def _make_handler(summaries=None, tasks=None, utterances=None, llm_response="結果"):
    from recall_handler import RecallHandler
    return RecallHandler(
        summary_store=_make_summary_store(summaries or []),
        task_store=_make_task_store(tasks or []),
        transcript_store=_make_transcript_store(utterances or []),
        groq_client=MagicMock(**{
            "chat.completions.create": AsyncMock(
                return_value=_make_groq_response(llm_response)
            )
        }),
        guild_id=1,
        owner_speaker="狗與鹿",
    )


# ── 待辦查詢路徑 ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_recall_pending_tasks_returns_list():
    tasks = [
        {"id": 1, "text": "查 API 文件", "direction": "inbound", "assignee": "狗與鹿",
         "status": "pending", "due_date": None, "source_quote": "我說要查",
         "source_window_start": time.time() - 300, "source_window_end": time.time(),
         "created_at": time.time()},
    ]
    handler = _make_handler(tasks=tasks)
    result = await handler.handle(speaker="狗與鹿", query="我今天還有什麼待辦")
    assert "API 文件" in result


@pytest.mark.asyncio
async def test_recall_no_pending_returns_empty_message():
    handler = _make_handler(tasks=[])
    result = await handler.handle(speaker="狗與鹿", query="我今天還有什麼待辦")
    assert result  # 不為空，給個友善訊息
    assert "沒有" in result or "待辦" in result or "空" in result


@pytest.mark.asyncio
async def test_recall_outbound_tasks_query():
    tasks = [
        {"id": 2, "text": "showay 確認合約", "direction": "outbound", "assignee": "showay",
         "status": "pending", "due_date": None, "source_quote": "我叫 showay 去查",
         "source_window_start": time.time() - 300, "source_window_end": time.time(),
         "created_at": time.time()},
    ]
    handler = _make_handler(tasks=tasks)
    result = await handler.handle(speaker="狗與鹿", query="我交辦給 showay 的那件事")
    assert "showay" in result or "合約" in result


# ── 摘要查詢路徑（情境層）→ 原始 STT 細節 ──────────────────

@pytest.mark.asyncio
async def test_recall_summary_then_raw_stt():
    now = time.time()
    summaries = [{
        "id": 1, "guild_id": 1,
        "window_start": now - 600, "window_end": now - 300,
        "summary_text": "Jack 提到要去爬山，並說下週要整理行程",
        "speakers": ["狗與鹿"],
        "created_at": now - 300,
    }]
    utterances = [
        {"speaker": "狗與鹿", "text": "對啊下週要去爬山，我要整理一下行程", "timestamp": now - 500},
    ]
    handler = _make_handler(
        summaries=summaries,
        utterances=utterances,
        llm_response="你在 10 分鐘前提到下週要去爬山，並說要整理行程。",
    )
    result = await handler.handle(speaker="狗與鹿", query="我剛才說過什麼")
    assert "爬山" in result or "行程" in result


@pytest.mark.asyncio
async def test_recall_no_summary_fallback_message():
    handler = _make_handler(summaries=[], utterances=[])
    result = await handler.handle(speaker="狗與鹿", query="我剛才說了什麼")
    assert result  # 不 crash，給友善訊息


# ── LLM 故障 graceful fallback ────────────────────────────

@pytest.mark.asyncio
async def test_recall_llm_failure_returns_fallback():
    import asyncio
    from recall_handler import RecallHandler
    now = time.time()
    summaries = [{"id": 1, "guild_id": 1, "window_start": now - 300, "window_end": now,
                  "summary_text": "某段對話", "speakers": ["狗與鹿"], "created_at": now}]
    handler = RecallHandler(
        summary_store=_make_summary_store(summaries),
        task_store=_make_task_store([]),
        transcript_store=_make_transcript_store([]),
        groq_client=MagicMock(**{
            "chat.completions.create": AsyncMock(side_effect=asyncio.TimeoutError())
        }),
        guild_id=1,
        owner_speaker="狗與鹿",
    )
    result = await handler.handle(speaker="狗與鹿", query="我剛才說了什麼")
    # 應該回傳摘要原文，而非 crash
    assert result
    assert "某段對話" in result or "找到" in result or "記得" in result


# ═══════════════════════════════════════════════════════
# multi-speaker 隔離：A 只查到 A 的任務
# ═══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_todo_query_only_returns_querying_speakers_tasks():
    """jack 查待辦，只看到自己的，不看到 alice 的。"""
    from recall_handler import RecallHandler
    from task_store import TaskStore
    from summary_store import SummaryStore

    task_store = TaskStore(db_path=":memory:")
    task_store.save_task(guild_id=1, text="jack 要做的事", direction="inbound",
                         assignee="jack", speaker="jack",
                         source_quote="jack說", source_window_start=0.0, source_window_end=300.0)
    task_store.save_task(guild_id=1, text="alice 要做的事", direction="inbound",
                         assignee="alice", speaker="alice",
                         source_quote="alice說", source_window_start=0.0, source_window_end=300.0)

    summary_store = SummaryStore(db_path=":memory:")
    transcript_store = MagicMock()
    groq_client = MagicMock()

    handler = RecallHandler(
        summary_store=summary_store,
        task_store=task_store,
        transcript_store=transcript_store,
        groq_client=groq_client,
        guild_id=1,
        owner_speaker="jack",
    )
    answer = await handler.handle(speaker="jack", query="我今天還有什麼待辦")
    assert "jack 要做的事" in answer
    assert "alice 要做的事" not in answer


@pytest.mark.asyncio
async def test_mark_done_only_affects_querying_speakers_tasks():
    """alice 說做完，只能標記 alice 自己的任務。"""
    from recall_handler import RecallHandler
    from task_store import TaskStore
    from summary_store import SummaryStore

    task_store = TaskStore(db_path=":memory:")
    jack_id = task_store.save_task(guild_id=1, text="jack 要做的事", direction="inbound",
                                   assignee="jack", speaker="jack",
                                   source_quote="jack說", source_window_start=0.0, source_window_end=300.0)
    task_store.save_task(guild_id=1, text="alice 要做的事", direction="inbound",
                         assignee="alice", speaker="alice",
                         source_quote="alice說", source_window_start=0.0, source_window_end=300.0)

    summary_store = SummaryStore(db_path=":memory:")
    handler = RecallHandler(
        summary_store=summary_store,
        task_store=task_store,
        transcript_store=MagicMock(),
        groq_client=MagicMock(),
        guild_id=1,
        owner_speaker="jack",
    )
    await handler.handle_mark_done(speaker="alice", query="做完了", status="done")
    # jack 的任務應仍是 pending
    jack_tasks = task_store.get_pending(guild_id=1, speaker="jack")
    assert len(jack_tasks) == 1
    assert jack_tasks[0]["id"] == jack_id


# ═══════════════════════════════════════════════════════
# is_recall_query — 精確度修正測試
# ═══════════════════════════════════════════════════════

# False positive：這些不應該觸發 recall
def test_recall_query_does_not_match_making_promise():
    from recall_handler import is_recall_query
    assert is_recall_query("我答應你去參加") is False

def test_recall_query_does_not_match_cooking():
    from recall_handler import is_recall_query
    assert is_recall_query("我要做飯") is False

def test_recall_query_does_not_match_stating_idea():
    from recall_handler import is_recall_query
    assert is_recall_query("我提到了一個想法") is False

# True positive：問句形式的承諾查詢應該觸發
def test_recall_query_matches_what_did_i_promise():
    from recall_handler import is_recall_query
    assert is_recall_query("我有沒有答應過什麼") is True

def test_recall_query_matches_what_did_i_commit():
    from recall_handler import is_recall_query
    assert is_recall_query("我答應了什麼") is True

def test_recall_query_matches_delegated_question():
    from recall_handler import is_recall_query
    assert is_recall_query("我交辦了什麼") is True

def test_recall_query_matches_what_else_todo():
    from recall_handler import is_recall_query
    assert is_recall_query("還有什麼事情要做") is True


# ═══════════════════════════════════════════════════════
# is_mark_done_query — 精確度修正測試
# ═══════════════════════════════════════════════════════

# False positive：「取消」不能攔截音樂/遊戲指令
def test_mark_done_does_not_match_cancel_song():
    from recall_handler import is_mark_done_query
    assert is_mark_done_query("取消那首歌") is False

def test_mark_done_does_not_match_plain_cancel():
    from recall_handler import is_mark_done_query
    assert is_mark_done_query("取消") is False

# True positive：新增常見完成說法
def test_mark_done_matches_handled():
    from recall_handler import is_mark_done_query
    assert is_mark_done_query("已經處理好了") is True

def test_mark_done_matches_solved():
    from recall_handler import is_mark_done_query
    assert is_mark_done_query("問題解決了") is True

def test_mark_done_matches_done_already():
    from recall_handler import is_mark_done_query
    assert is_mark_done_query("辦好了") is True

# 原有的應該繼續 pass
def test_mark_done_still_matches_gaoding():
    from recall_handler import is_mark_done_query
    assert is_mark_done_query("搞定了") is True

def test_mark_done_still_matches_wancheng():
    from recall_handler import is_mark_done_query
    assert is_mark_done_query("完成了") is True


# ═══════════════════════════════════════════════════════
# is_manual_add_query
# ═══════════════════════════════════════════════════════

def test_manual_add_detects_remember_this():
    from recall_handler import is_manual_add_query
    assert is_manual_add_query("記一下，我等等要買菜") is True

def test_manual_add_detects_help_me_note():
    from recall_handler import is_manual_add_query
    assert is_manual_add_query("幫我記，下週要寄報告") is True

def test_manual_add_detects_add_todo():
    from recall_handler import is_manual_add_query
    assert is_manual_add_query("加一個待辦：確認 showay 合約") is True

def test_manual_add_does_not_match_recall_query():
    from recall_handler import is_manual_add_query
    assert is_manual_add_query("我剛才說了什麼") is False

def test_manual_add_does_not_match_normal_chat():
    from recall_handler import is_manual_add_query
    assert is_manual_add_query("今天天氣不錯") is False


# ═══════════════════════════════════════════════════════
# is_yes_response / is_no_response
# ═══════════════════════════════════════════════════════

def test_yes_response_detects_dui():
    from recall_handler import is_yes_response
    assert is_yes_response("對") is True

def test_yes_response_detects_hao():
    from recall_handler import is_yes_response
    assert is_yes_response("好，記下去") is True

def test_yes_response_detects_shi():
    from recall_handler import is_yes_response
    assert is_yes_response("是") is True

def test_no_response_detects_buyong():
    from recall_handler import is_no_response
    assert is_no_response("不用") is True

def test_no_response_detects_suanle():
    from recall_handler import is_no_response
    assert is_no_response("算了") is True

def test_no_response_detects_buyao():
    from recall_handler import is_no_response
    assert is_no_response("不要記") is True

def test_yes_response_does_not_match_unrelated():
    from recall_handler import is_yes_response
    assert is_yes_response("我去買菜") is False


# ═══════════════════════════════════════════════════════
# is_task_update_query
# ═══════════════════════════════════════════════════════

def test_task_update_detects_gaicheng():
    from recall_handler import is_task_update_query
    assert is_task_update_query("那件事改成我自己處理") is True

def test_task_update_detects_mubiao_huan():
    from recall_handler import is_task_update_query
    assert is_task_update_query("目標換成先暫停") is True

def test_task_update_detects_fangxiang_bian():
    from recall_handler import is_task_update_query
    assert is_task_update_query("方向變了，不找 showay 了") is True

def test_task_update_does_not_match_normal():
    from recall_handler import is_task_update_query
    assert is_task_update_query("我要去買菜") is False


# ═══════════════════════════════════════════════════════
# handle_manual_add：立即存入，不等 SessionSummarizer
# ═══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_handle_manual_add_saves_task_immediately():
    from recall_handler import RecallHandler
    from task_store import TaskStore
    from summary_store import SummaryStore

    task_store = TaskStore(db_path=":memory:")
    handler = RecallHandler(
        summary_store=SummaryStore(db_path=":memory:"),
        task_store=task_store,
        transcript_store=MagicMock(),
        groq_client=MagicMock(),
        guild_id=1,
        owner_speaker="jack",
    )
    await handler.handle_manual_add(speaker="jack", query="記一下，下週要寄報告給 showay")
    pending = task_store.get_pending(guild_id=1, speaker="jack")
    assert len(pending) == 1

@pytest.mark.asyncio
async def test_handle_manual_add_strips_trigger_phrase():
    from recall_handler import RecallHandler
    from task_store import TaskStore
    from summary_store import SummaryStore

    task_store = TaskStore(db_path=":memory:")
    handler = RecallHandler(
        summary_store=SummaryStore(db_path=":memory:"),
        task_store=task_store,
        transcript_store=MagicMock(),
        groq_client=MagicMock(),
        guild_id=1,
        owner_speaker="jack",
    )
    await handler.handle_manual_add(speaker="jack", query="記一下，等等要買菜")
    pending = task_store.get_pending(guild_id=1, speaker="jack")
    assert "記一下" not in pending[0]["text"]
    assert "買菜" in pending[0]["text"]


# ═══════════════════════════════════════════════════════
# handle_task_update：更新已有任務內容
# ═══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_handle_task_update_by_keyword():
    from recall_handler import RecallHandler
    from task_store import TaskStore
    from summary_store import SummaryStore

    task_store = TaskStore(db_path=":memory:")
    task_store.save_task(guild_id=1, text="找 showay 確認合約", direction="outbound",
                         assignee="showay", speaker="jack",
                         source_quote="", source_window_start=0.0, source_window_end=300.0)

    handler = RecallHandler(
        summary_store=SummaryStore(db_path=":memory:"),
        task_store=task_store,
        transcript_store=MagicMock(),
        groq_client=MagicMock(),
        guild_id=1,
        owner_speaker="jack",
    )
    await handler.handle_task_update(speaker="jack", query="合約的事改成我自己去談")
    pending = task_store.get_pending(guild_id=1, speaker="jack")
    assert "我自己去談" in pending[0]["text"]

@pytest.mark.asyncio
async def test_handle_task_update_by_last_task_id():
    from recall_handler import RecallHandler
    from task_store import TaskStore
    from summary_store import SummaryStore

    task_store = TaskStore(db_path=":memory:")
    task_id = task_store.save_task(guild_id=1, text="買菜", direction="inbound",
                                   assignee="jack", speaker="jack",
                                   source_quote="", source_window_start=0.0, source_window_end=300.0)

    handler = RecallHandler(
        summary_store=SummaryStore(db_path=":memory:"),
        task_store=task_store,
        transcript_store=MagicMock(),
        groq_client=MagicMock(),
        guild_id=1,
        owner_speaker="jack",
    )
    await handler.handle_task_update(
        speaker="jack", query="那件事改成買咖啡豆", last_task_id=task_id
    )
    pending = task_store.get_pending(guild_id=1, speaker="jack")
    assert "咖啡豆" in pending[0]["text"]


@pytest.mark.asyncio
async def test_handle_manual_add_sets_last_task_id():
    """handle_manual_add 儲存後應更新 handler.last_task_id，供「那件事」解析。"""
    from recall_handler import RecallHandler
    from task_store import TaskStore
    from summary_store import SummaryStore

    task_store = TaskStore(db_path=":memory:")
    handler = RecallHandler(
        summary_store=SummaryStore(db_path=":memory:"),
        task_store=task_store,
        transcript_store=MagicMock(),
        groq_client=MagicMock(),
        guild_id=1,
        owner_speaker="jack",
    )
    assert handler.last_task_id is None
    await handler.handle_manual_add(speaker="jack", query="記一下，等等去買咖啡")
    assert handler.last_task_id is not None
    assert isinstance(handler.last_task_id, int)
