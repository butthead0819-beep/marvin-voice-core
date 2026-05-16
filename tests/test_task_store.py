"""
TDD tests for TaskStore — voice-native task manager backing store.

Run with:
    pytest tests/test_task_store.py -v
"""
from __future__ import annotations

import time
import pytest


def _make_store(db_path: str = ":memory:"):
    from task_store import TaskStore
    return TaskStore(db_path=db_path)


# ── 1. save & get_pending ────────────────────────────────────────────────────

def test_save_inbound_task_appears_in_pending():
    store = _make_store()
    store.save_task(
        guild_id=1,
        text="查一下那個工具的文件",
        direction="inbound",
        assignee="狗與鹿",
        source_quote="我等等要記得查一下那個工具的文件",
        source_window_start=1000.0,
        source_window_end=1300.0,
    )
    pending = store.get_pending(guild_id=1)
    assert len(pending) == 1
    assert pending[0]["text"] == "查一下那個工具的文件"
    assert pending[0]["direction"] == "inbound"
    assert pending[0]["status"] == "pending"


def test_save_outbound_task_appears_in_pending():
    store = _make_store()
    store.save_task(
        guild_id=1,
        text="showay 去確認合約條款",
        direction="outbound",
        assignee="showay",
        source_quote="我叫 showay 去確認合約條款",
        source_window_start=1000.0,
        source_window_end=1300.0,
    )
    pending = store.get_pending(guild_id=1)
    assert len(pending) == 1
    assert pending[0]["assignee"] == "showay"
    assert pending[0]["direction"] == "outbound"


def test_get_pending_filters_by_direction():
    store = _make_store()
    store.save_task(guild_id=1, text="我要做A", direction="inbound", assignee="狗與鹿",
                    source_quote="", source_window_start=0.0, source_window_end=300.0)
    store.save_task(guild_id=1, text="showay 要做B", direction="outbound", assignee="showay",
                    source_quote="", source_window_start=0.0, source_window_end=300.0)

    inbound = store.get_pending(guild_id=1, direction="inbound")
    outbound = store.get_pending(guild_id=1, direction="outbound")

    assert len(inbound) == 1 and inbound[0]["direction"] == "inbound"
    assert len(outbound) == 1 and outbound[0]["direction"] == "outbound"


def test_get_pending_excludes_other_guild():
    store = _make_store()
    store.save_task(guild_id=1, text="任務A", direction="inbound", assignee="狗與鹿",
                    source_quote="", source_window_start=0.0, source_window_end=300.0)
    assert store.get_pending(guild_id=2) == []


# ── 2. update_status ─────────────────────────────────────────────────────────

def test_update_status_done_removes_from_pending():
    store = _make_store()
    store.save_task(guild_id=1, text="寄出那封信", direction="inbound", assignee="狗與鹿",
                    source_quote="我等等寄出那封信", source_window_start=0.0, source_window_end=300.0)
    task_id = store.get_pending(guild_id=1)[0]["id"]

    store.update_status(task_id, "done")

    assert store.get_pending(guild_id=1) == []


def test_update_status_cancelled_removes_from_pending():
    store = _make_store()
    store.save_task(guild_id=1, text="買咖啡豆", direction="inbound", assignee="狗與鹿",
                    source_quote="", source_window_start=0.0, source_window_end=300.0)
    task_id = store.get_pending(guild_id=1)[0]["id"]

    store.update_status(task_id, "cancelled")

    assert store.get_pending(guild_id=1) == []


def test_update_status_invalid_raises():
    store = _make_store()
    with pytest.raises(ValueError):
        store.update_status(task_id=999, status="flying")


# ── 3. due_date & overdue ────────────────────────────────────────────────────

def test_save_task_with_due_date():
    store = _make_store()
    due = time.time() + 3600  # 一小時後
    store.save_task(guild_id=1, text="開會前準備簡報", direction="inbound", assignee="狗與鹿",
                    source_quote="", source_window_start=0.0, source_window_end=300.0,
                    due_date=due)
    pending = store.get_pending(guild_id=1)
    assert abs(pending[0]["due_date"] - due) < 1


def test_get_overdue_returns_past_due_pending_tasks():
    store = _make_store()
    past = time.time() - 3600  # 一小時前
    store.save_task(guild_id=1, text="昨天忘了的事", direction="inbound", assignee="狗與鹿",
                    source_quote="", source_window_start=0.0, source_window_end=300.0,
                    due_date=past)
    overdue = store.get_overdue(guild_id=1)
    assert len(overdue) == 1
    assert overdue[0]["text"] == "昨天忘了的事"


def test_get_overdue_excludes_done_tasks():
    store = _make_store()
    past = time.time() - 3600
    store.save_task(guild_id=1, text="已完成的過期任務", direction="inbound", assignee="狗與鹿",
                    source_quote="", source_window_start=0.0, source_window_end=300.0,
                    due_date=past)
    task_id = store.get_pending(guild_id=1)[0]["id"]
    store.update_status(task_id, "done")

    assert store.get_overdue(guild_id=1) == []


def test_get_overdue_excludes_future_due():
    store = _make_store()
    future = time.time() + 3600
    store.save_task(guild_id=1, text="還沒到期", direction="inbound", assignee="狗與鹿",
                    source_quote="", source_window_start=0.0, source_window_end=300.0,
                    due_date=future)
    assert store.get_overdue(guild_id=1) == []


def test_get_overdue_excludes_tasks_without_due_date():
    store = _make_store()
    store.save_task(guild_id=1, text="沒有截止日", direction="inbound", assignee="狗與鹿",
                    source_quote="", source_window_start=0.0, source_window_end=300.0)
    assert store.get_overdue(guild_id=1) == []


# ── 4. search ────────────────────────────────────────────────────────────────

def test_search_finds_task_by_keyword():
    store = _make_store()
    store.save_task(guild_id=1, text="查 OpenAI API 文件", direction="inbound", assignee="狗與鹿",
                    source_quote="", source_window_start=0.0, source_window_end=300.0)
    store.save_task(guild_id=1, text="買早餐", direction="inbound", assignee="狗與鹿",
                    source_quote="", source_window_start=0.0, source_window_end=300.0)

    results = store.search(guild_id=1, keyword="API")
    assert len(results) == 1
    assert "API" in results[0]["text"]


def test_search_also_matches_source_quote():
    store = _make_store()
    store.save_task(guild_id=1, text="整理那個東西", direction="inbound", assignee="狗與鹿",
                    source_quote="我等等要把那個 API key 整理一下",
                    source_window_start=0.0, source_window_end=300.0)

    results = store.search(guild_id=1, keyword="API key")
    assert len(results) == 1


def test_search_returns_empty_when_no_match():
    store = _make_store()
    store.save_task(guild_id=1, text="買咖啡", direction="inbound", assignee="狗與鹿",
                    source_quote="", source_window_start=0.0, source_window_end=300.0)
    assert store.search(guild_id=1, keyword="拉麵") == []


# ── 5. get_by_window ─────────────────────────────────────────────────────────

def test_get_by_window_returns_tasks_in_range():
    store = _make_store()
    store.save_task(guild_id=1, text="窗口內的任務", direction="inbound", assignee="狗與鹿",
                    source_quote="", source_window_start=1000.0, source_window_end=1300.0)
    store.save_task(guild_id=1, text="窗口外的任務", direction="inbound", assignee="狗與鹿",
                    source_quote="", source_window_start=2000.0, source_window_end=2300.0)

    results = store.get_by_window(guild_id=1, window_start=900.0, window_end=1400.0)
    assert len(results) == 1
    assert results[0]["text"] == "窗口內的任務"


# ── 6. source_quote 保留原話 ──────────────────────────────────────────────────

def test_source_quote_is_preserved_exactly():
    store = _make_store()
    quote = "我說我等等去買那個、然後順便查一下文件，應該不會太久"
    store.save_task(guild_id=1, text="買東西查文件", direction="inbound", assignee="狗與鹿",
                    source_quote=quote, source_window_start=0.0, source_window_end=300.0)

    pending = store.get_pending(guild_id=1)
    assert pending[0]["source_quote"] == quote


# ── 7. multi-speaker 隔離 ─────────────────────────────────────────────────────

def test_get_pending_with_speaker_filter_returns_only_that_speaker():
    store = _make_store()
    store.save_task(guild_id=1, text="jack 的任務", direction="inbound", assignee="jack",
                    speaker="jack", source_quote="jack說", source_window_start=0.0, source_window_end=300.0)
    store.save_task(guild_id=1, text="alice 的任務", direction="inbound", assignee="alice",
                    speaker="alice", source_quote="alice說", source_window_start=0.0, source_window_end=300.0)
    result = store.get_pending(guild_id=1, speaker="jack")
    assert len(result) == 1
    assert result[0]["text"] == "jack 的任務"


def test_get_pending_no_speaker_filter_returns_all():
    store = _make_store()
    store.save_task(guild_id=1, text="jack 的任務", direction="inbound", assignee="jack",
                    speaker="jack", source_quote="jack說", source_window_start=0.0, source_window_end=300.0)
    store.save_task(guild_id=1, text="alice 的任務", direction="inbound", assignee="alice",
                    speaker="alice", source_quote="alice說", source_window_start=0.0, source_window_end=300.0)
    result = store.get_pending(guild_id=1)
    assert len(result) == 2


def test_search_with_speaker_filter_returns_only_that_speaker():
    store = _make_store()
    store.save_task(guild_id=1, text="買拉麵材料", direction="inbound", assignee="jack",
                    speaker="jack", source_quote="jack要買拉麵", source_window_start=0.0, source_window_end=300.0)
    store.save_task(guild_id=1, text="買咖啡豆", direction="inbound", assignee="alice",
                    speaker="alice", source_quote="alice要買咖啡", source_window_start=0.0, source_window_end=300.0)
    result = store.search(guild_id=1, keyword="買", speaker="alice")
    assert len(result) == 1
    assert result[0]["text"] == "買咖啡豆"


def test_speaker_field_stored_and_returned():
    store = _make_store()
    store.save_task(guild_id=1, text="某件事", direction="inbound", assignee="jack",
                    speaker="jack", source_quote="jack說某件事", source_window_start=0.0, source_window_end=300.0)
    pending = store.get_pending(guild_id=1, speaker="jack")
    assert pending[0]["speaker"] == "jack"


# ── 8. update_text + get_done ─────────────────────────────────────────────────

def test_update_text_changes_task_content():
    store = _make_store()
    task_id = store.save_task(guild_id=1, text="原本的任務", direction="inbound",
                              assignee="jack", speaker="jack",
                              source_quote="jack說", source_window_start=0.0, source_window_end=300.0)
    store.update_text(task_id, "更新後的目標")
    pending = store.get_pending(guild_id=1)
    assert pending[0]["text"] == "更新後的目標"


def test_update_text_does_not_change_status():
    store = _make_store()
    task_id = store.save_task(guild_id=1, text="某任務", direction="inbound",
                              assignee="jack", speaker="jack",
                              source_quote="", source_window_start=0.0, source_window_end=300.0)
    store.update_text(task_id, "改過的任務")
    pending = store.get_pending(guild_id=1)
    assert pending[0]["status"] == "pending"


def test_get_done_returns_completed_tasks():
    store = _make_store()
    task_id = store.save_task(guild_id=1, text="完成的任務", direction="inbound",
                              assignee="jack", speaker="jack",
                              source_quote="", source_window_start=0.0, source_window_end=300.0)
    store.update_status(task_id, "done")
    done = store.get_done(guild_id=1, hours=24)
    assert len(done) == 1
    assert done[0]["text"] == "完成的任務"


def test_get_done_excludes_pending():
    store = _make_store()
    store.save_task(guild_id=1, text="未完成", direction="inbound",
                    assignee="jack", speaker="jack",
                    source_quote="", source_window_start=0.0, source_window_end=300.0)
    assert store.get_done(guild_id=1, hours=24) == []


def test_get_done_filters_by_speaker():
    store = _make_store()
    id1 = store.save_task(guild_id=1, text="jack的任務", direction="inbound",
                          assignee="jack", speaker="jack",
                          source_quote="", source_window_start=0.0, source_window_end=300.0)
    id2 = store.save_task(guild_id=1, text="alice的任務", direction="inbound",
                          assignee="alice", speaker="alice",
                          source_quote="", source_window_start=0.0, source_window_end=300.0)
    store.update_status(id1, "done")
    store.update_status(id2, "done")
    done = store.get_done(guild_id=1, speaker="jack", hours=24)
    assert len(done) == 1
    assert done[0]["text"] == "jack的任務"
