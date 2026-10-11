"""event_memory.py — 事件記憶 Phase 2：事件過後追問。

pick_followups 時間窗/在場/隱私濾網 + render_followup_line/material 措辭 +
EventStore.list_followup_candidates/mark_followed_up/prune 的事件追問行為。
"""
from __future__ import annotations

import time

import pytest

from event_memory import (
    FOLLOWUP_MAX_DAYS_AFTER,
    EventStore,
    pick_followups,
    render_followup_line,
    render_followup_material,
)

NOW = 2_000_000_000.0
DAY = 86400.0


def _ev(event_id=1, owner="showay", activity="露營", event_date=None, heard_by=None):
    return {
        "id": event_id, "guild_id": 1, "owner": owner, "activity": activity,
        "place": None, "event_date": event_date, "date_text": "",
        "participants": [], "heard_by": heard_by if heard_by is not None else [owner],
        "quotes": [], "first_seen": NOW, "last_seen": NOW, "created_at": NOW,
    }


# ═══════════════════════════════════════════════════════
# pick_followups
# ═══════════════════════════════════════════════════════

def test_not_due_before_day_ends():
    ev = _ev(event_date=NOW - DAY + 1)  # 不滿一天
    assert pick_followups([ev], ["showay"], NOW) == []


def test_due_right_after_day_ends():
    ev = _ev(event_date=NOW - DAY)
    assert pick_followups([ev], ["showay"], NOW) == [ev]


def test_due_at_max_days_boundary():
    ev = _ev(event_date=NOW - (FOLLOWUP_MAX_DAYS_AFTER + 1) * DAY)
    assert pick_followups([ev], ["showay"], NOW) == [ev]


def test_not_due_past_max_days_boundary():
    ev = _ev(event_date=NOW - (FOLLOWUP_MAX_DAYS_AFTER + 1) * DAY - 1)
    assert pick_followups([ev], ["showay"], NOW) == []


def test_event_date_none_excluded():
    ev = _ev(event_date=None)
    assert pick_followups([ev], ["showay"], NOW) == []


def test_owner_not_present_excluded():
    ev = _ev(owner="showay", event_date=NOW - 2 * DAY)
    assert pick_followups([ev], ["狗與露"], NOW) == []


def test_present_not_subset_of_heard_by_excluded():
    ev = _ev(owner="showay", event_date=NOW - 2 * DAY, heard_by=["showay"])
    assert pick_followups([ev], ["showay", "狗與露"], NOW) == []


def test_present_empty_returns_empty():
    ev = _ev(event_date=NOW - 2 * DAY)
    assert pick_followups([ev], [], NOW) == []


def test_multiple_events_preserve_input_order():
    ev1 = _ev(event_id=1, owner="showay", event_date=NOW - 2 * DAY)
    ev2 = _ev(event_id=2, owner="showay", event_date=NOW - 3 * DAY)
    assert pick_followups([ev1, ev2], ["showay"], NOW) == [ev1, ev2]
    assert pick_followups([ev2, ev1], ["showay"], NOW) == [ev2, ev1]


# ═══════════════════════════════════════════════════════
# render_followup_line / render_followup_material
# ═══════════════════════════════════════════════════════

def test_render_followup_line_content():
    ev = _ev(owner="showay", activity="露營", event_date=NOW - 2 * DAY)
    line = render_followup_line(ev)
    assert "showay" in line
    assert "露營" in line
    assert "後來怎麼樣" in line
    assert "好玩" not in line
    assert "「" not in line and "」" not in line  # 不念原話 quote


def test_render_followup_material_content():
    ev = _ev(owner="showay", activity="露營", event_date=NOW - 2 * DAY)
    material = render_followup_material(ev)
    assert material.startswith("showay 之前說")
    assert "要露營" in material


# ═══════════════════════════════════════════════════════
# EventStore
# ═══════════════════════════════════════════════════════

def test_list_followup_candidates_excludes_null_date_marked_and_other_guild():
    store = EventStore(":memory:")
    id_null = store.add(1, {"owner": "showay", "activity": "露營", "place": None,
                             "event_date": None, "date_text": "", "participants": [], "quote": ""},
                         ["showay"], NOW)
    id_marked = store.add(1, {"owner": "showay", "activity": "出國", "place": None,
                               "event_date": NOW - 5 * DAY, "date_text": "", "participants": [], "quote": ""},
                           ["showay"], NOW)
    id_other_guild = store.add(2, {"owner": "showay", "activity": "爬山", "place": None,
                                    "event_date": NOW - 3 * DAY, "date_text": "", "participants": [], "quote": ""},
                                ["showay"], NOW)
    id_ok = store.add(1, {"owner": "showay", "activity": "聚餐", "place": None,
                           "event_date": NOW - 1 * DAY, "date_text": "", "participants": [], "quote": ""},
                       ["showay"], NOW)
    store.mark_followed_up(id_marked, NOW)

    candidates = store.list_followup_candidates(1)
    ids = [c["id"] for c in candidates]
    assert id_ok in ids
    assert id_null not in ids
    assert id_marked not in ids
    assert id_other_guild not in ids


def test_list_followup_candidates_orders_by_event_date_desc():
    store = EventStore(":memory:")
    older = store.add(1, {"owner": "showay", "activity": "A", "place": None,
                           "event_date": NOW - 10 * DAY, "date_text": "", "participants": [], "quote": ""},
                       ["showay"], NOW)
    newer = store.add(1, {"owner": "showay", "activity": "B", "place": None,
                           "event_date": NOW - 1 * DAY, "date_text": "", "participants": [], "quote": ""},
                       ["showay"], NOW)
    ids = [c["id"] for c in store.list_followup_candidates(1)]
    assert ids == [newer, older]


def test_mark_followed_up_idempotent():
    store = EventStore(":memory:")
    eid = store.add(1, {"owner": "showay", "activity": "A", "place": None,
                         "event_date": NOW - 1 * DAY, "date_text": "", "participants": [], "quote": ""},
                     ["showay"], NOW)
    store.mark_followed_up(eid, NOW)
    store.mark_followed_up(eid, NOW)  # 不報錯
    assert store.list_followup_candidates(1) == []


def test_prune_removes_orphan_followups():
    store = EventStore(":memory:")
    eid = store.add(1, {"owner": "showay", "activity": "A", "place": None,
                         "event_date": NOW - 1 * DAY, "date_text": "", "participants": [], "quote": ""},
                     ["showay"], NOW - 100 * DAY)
    store.mark_followed_up(eid, NOW - 100 * DAY)
    removed = store.prune(retention_days=30, now=NOW)
    assert removed == 1
    con = store._connect()
    rows = con.execute("SELECT * FROM event_followups").fetchall()
    assert rows == []


def test_render_followup_line_literal():
    # 2026-10-04 00:00 台北
    ev = _ev(owner="showay", activity="露營", event_date=1791043200.0)
    assert render_followup_line(ev) == "對了showay，你之前說10月4日要露營，後來怎麼樣？"
    assert render_followup_material(ev) == "showay 之前說10月4日要露營"
