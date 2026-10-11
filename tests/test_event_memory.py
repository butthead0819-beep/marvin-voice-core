"""event_memory.py — 事件記憶 Phase 1。

EventStore CRUD/merge/prune + 純函式（parse_events/validate_event/find_merge_target/
visible_events/parse_ids/render_answer）+ extract_and_store 整合。
"""
from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from event_memory import (
    EXTRACT_SYSTEM_PROMPT,
    EventStore,
    NOT_REMEMBER,
    _activity_similarity,
    _md,
    _normalize,
    _quote_speaker,
    build_extract_user_prompt,
    build_select_user_prompt,
    extract_and_store,
    find_merge_target,
    learn_hometowns_local,
    parse_events,
    parse_hometowns,
    parse_ids,
    render_answer,
    validate_event,
    validate_hometown,
    visible_events,
)



# ═══════════════════════════════════════════════════════
# EventStore
# ═══════════════════════════════════════════════════════

def _ev(owner="showay", activity="露營", place="溪頭", event_date=None, date_text="", participants=None, quote=""):
    return {
        "owner": owner, "activity": activity, "place": place, "event_date": event_date,
        "date_text": date_text, "participants": participants or [], "quote": quote,
    }


def test_add_then_get_roundtrip():
    store = EventStore(":memory:")
    now = time.time()
    eid = store.add(1, _ev(quote="我要去溪頭露營"), ["showay"], now)
    row = store.get(eid)
    assert row["owner"] == "showay"
    assert row["activity"] == "露營"
    assert row["place"] == "溪頭"
    assert row["participants"] == []
    assert row["heard_by"] == ["showay"]
    assert row["quotes"] == ["我要去溪頭露營"]
    assert row["first_seen"] == now
    assert row["last_seen"] == now


def test_add_without_quote_gives_empty_quotes():
    store = EventStore(":memory:")
    eid = store.add(1, _ev(), ["showay"], time.time())
    assert store.get(eid)["quotes"] == []


def test_get_missing_id_returns_none():
    store = EventStore(":memory:")
    assert store.get(999) is None


def test_recent_for_owners_filters_owner_since_order_limit():
    store = EventStore(":memory:")
    now = time.time()
    id_a = store.add(1, _ev(owner="showay", activity="露營"), ["showay"], now - 100)
    id_b = store.add(1, _ev(owner="showay", activity="出國"), ["showay"], now - 50)
    store.add(1, _ev(owner="狗與露", activity="搬家"), ["狗與露"], now)  # 不同 owner
    store.add(1, _ev(owner="showay", activity="太舊"), ["showay"], now - 10000)  # since 之外

    results = store.recent_for_owners(1, ["showay"], since_ts=now - 200, limit=10)
    ids = [r["id"] for r in results]
    assert ids == [id_b, id_a]  # last_seen DESC


def test_recent_for_owners_empty_owners_returns_empty_without_query():
    store = EventStore(":memory:")
    store.add(1, _ev(), ["showay"], time.time())
    assert store.recent_for_owners(1, [], time.time() - 100) == []


def test_list_guild_filters_since_and_orders():
    store = EventStore(":memory:")
    now = time.time()
    id_old = store.add(1, _ev(activity="太舊"), ["showay"], now - 1000)
    id_new = store.add(1, _ev(activity="新"), ["showay"], now)
    results = store.list_guild(1, now - 10)
    assert [r["id"] for r in results] == [id_new]
    results_all = store.list_guild(1, now - 2000)
    assert [r["id"] for r in results_all] == [id_new, id_old]


def test_prune_deletes_old_keeps_new():
    store = EventStore(":memory:")
    now = time.time()
    store.add(1, _ev(), ["showay"], now - 31 * 86400)
    store.add(1, _ev(), ["showay"], now - 1 * 86400)
    deleted = store.prune(retention_days=30, now=now)
    assert deleted == 1
    assert len(store.list_guild(1, 0)) == 1


# ── merge ──────────────────────────────────────────────

def test_merge_heard_by_and_participants_union_preserves_order():
    store = EventStore(":memory:")
    now = time.time()
    eid = store.add(1, _ev(participants=["Jack"]), ["showay"], now)
    store.merge(eid, _ev(participants=["Jack", "Mary"]), ["showay", "狗與露"], now + 10)
    row = store.get(eid)
    assert row["heard_by"] == ["showay", "狗與露"]
    assert row["participants"] == ["Jack", "Mary"]


def test_merge_duplicate_quote_not_added():
    store = EventStore(":memory:")
    now = time.time()
    eid = store.add(1, _ev(quote="去露營"), ["showay"], now)
    store.merge(eid, _ev(quote="去露營"), ["showay"], now + 10)
    assert store.get(eid)["quotes"] == ["去露營"]


def test_merge_sixth_quote_evicts_oldest():
    store = EventStore(":memory:")
    now = time.time()
    eid = store.add(1, _ev(quote="q1"), ["showay"], now)
    for i in range(2, 7):
        store.merge(eid, _ev(quote=f"q{i}"), ["showay"], now + i)
    quotes = store.get(eid)["quotes"]
    assert quotes == ["q2", "q3", "q4", "q5", "q6"]
    assert len(quotes) == 5


def test_merge_place_and_date_none_does_not_overwrite():
    store = EventStore(":memory:")
    now = time.time()
    eid = store.add(1, _ev(place="溪頭", event_date=now), ["showay"], now)
    store.merge(eid, _ev(place=None, event_date=None), ["showay"], now + 10)
    row = store.get(eid)
    assert row["place"] == "溪頭"
    assert row["event_date"] == now


def test_merge_place_and_date_non_none_overwrites():
    store = EventStore(":memory:")
    now = time.time()
    eid = store.add(1, _ev(place="溪頭", event_date=now), ["showay"], now)
    store.merge(eid, _ev(place="阿里山", event_date=now + 1), ["showay"], now + 10)
    row = store.get(eid)
    assert row["place"] == "阿里山"
    assert row["event_date"] == now + 1


def test_merge_date_text_empty_does_not_overwrite():
    store = EventStore(":memory:")
    now = time.time()
    eid = store.add(1, _ev(date_text="下禮拜"), ["showay"], now)
    store.merge(eid, _ev(date_text=""), ["showay"], now + 10)
    assert store.get(eid)["date_text"] == "下禮拜"


def test_merge_last_seen_takes_max():
    store = EventStore(":memory:")
    now = time.time()
    eid = store.add(1, _ev(), ["showay"], now)
    store.merge(eid, _ev(), ["showay"], now - 100)  # 較舊時間，不該回退
    assert store.get(eid)["last_seen"] == now


def test_merge_missing_id_does_not_raise():
    store = EventStore(":memory:")
    store.merge(9999, _ev(), ["showay"], time.time())  # 不該 raise


# ═══════════════════════════════════════════════════════
# parse_events
# ═══════════════════════════════════════════════════════

def test_parse_events_plain_json():
    assert parse_events('{"events":[{"owner":"a"}]}') == [{"owner": "a"}]


def test_parse_events_code_fence():
    content = '```json\n{"events":[{"owner":"a"}]}\n```'
    assert parse_events(content) == [{"owner": "a"}]


def test_parse_events_bad_json_returns_empty():
    assert parse_events("not json") == []


def test_parse_events_non_list_events_returns_empty():
    assert parse_events('{"events":"nope"}') == []


def test_parse_events_filters_non_dict_elements():
    assert parse_events('{"events":[{"owner":"a"}, "junk", 1]}') == [{"owner": "a"}]


# ═══════════════════════════════════════════════════════
# validate_event
# ═══════════════════════════════════════════════════════

_TRANSCRIPT = "showay 說要去溪頭露營，date_text 是下禮拜六，引述句：下禮拜六我要去溪頭露營"


def test_validate_event_owner_not_in_heard_by_rejected():
    raw = {"owner": "狗與露", "activity": "露營", "subject": "狗與露"}
    assert validate_event(raw, ["showay"], _TRANSCRIPT, time.time()) is None


@pytest.mark.parametrize("name", ["馬文", "Marvin", "marvin"])
def test_validate_event_marvin_rejected(name):
    raw = {"owner": name, "activity": "露營", "subject": name}
    assert validate_event(raw, [name], _TRANSCRIPT, time.time()) is None


def test_validate_event_empty_activity_rejected():
    raw = {"owner": "showay", "activity": "", "subject": "showay"}
    assert validate_event(raw, ["showay"], _TRANSCRIPT, time.time()) is None


def test_validate_event_activity_too_long_rejected():
    raw = {"owner": "showay", "activity": "一二三四五六七八九十十一十二十三", "subject": "showay"}
    assert validate_event(raw, ["showay"], _TRANSCRIPT, time.time()) is None


def test_validate_event_date_in_range_converted_to_taipei_midnight():
    window_end = 1791640000.0  # 2026-10-10 21:46 台北（固定值，避免牆鐘讓測試過期）
    raw = {"owner": "showay", "activity": "露營", "date": "2026-10-11", "subject": "showay"}
    ev = validate_event(raw, ["showay"], _TRANSCRIPT, window_end)
    assert ev is not None
    assert ev["event_date"] == 1791648000.0  # 2026-10-11 00:00 台北


def test_validate_event_date_too_far_future_kept_but_date_none():
    window_end = 1791640000.0  # 2026-10-10；2030-01-01 超過 +365 天
    raw = {"owner": "showay", "activity": "露營", "date": "2030-01-01", "subject": "showay"}
    ev = validate_event(raw, ["showay"], _TRANSCRIPT, window_end)
    assert ev is not None
    assert ev["event_date"] is None


def test_validate_event_date_relative_word_invalid_format_is_none():
    raw = {"owner": "showay", "activity": "露營", "date": "明天", "subject": "showay"}
    ev = validate_event(raw, ["showay"], _TRANSCRIPT, time.time())
    assert ev["event_date"] is None


def test_validate_event_place_found_with_punct_variation_kept():
    raw = {"owner": "showay", "activity": "露營", "place": "溪頭", "subject": "showay"}
    ev = validate_event(raw, ["showay"], _TRANSCRIPT, time.time())
    assert ev["place"] == "溪頭"


def test_validate_event_place_not_in_transcript_is_none():
    raw = {"owner": "showay", "activity": "露營", "place": "日本", "subject": "showay"}
    ev = validate_event(raw, ["showay"], _TRANSCRIPT, time.time())
    assert ev["place"] is None


def test_validate_event_quote_not_in_transcript_is_empty():
    raw = {"owner": "showay", "activity": "露營", "quote": "完全不相關的句子", "subject": "showay"}
    ev = validate_event(raw, ["showay"], _TRANSCRIPT, time.time())
    assert ev["quote"] == ""


def test_validate_event_quote_in_transcript_kept():
    raw = {"owner": "showay", "activity": "露營", "quote": "下禮拜六我要去溪頭露營", "subject": "showay"}
    ev = validate_event(raw, ["showay"], _TRANSCRIPT, time.time())
    assert ev["quote"] == "下禮拜六我要去溪頭露營"


@pytest.mark.parametrize("same_as,expected", [("12", 12), ("abc", None), (True, None)])
def test_validate_event_same_as_variants(same_as, expected):
    raw = {"owner": "showay", "activity": "露營", "same_as": same_as, "subject": "showay"}
    ev = validate_event(raw, ["showay"], _TRANSCRIPT, time.time())
    assert ev["same_as"] == expected


# ═══════════════════════════════════════════════════════
# validate_event — subject 判定主角歸屬（owner 轉述歸屬錯修正）
# ═══════════════════════════════════════════════════════

_SUBJECT_TRANSCRIPT = (
    "[15:24:57] showay: 我媽是腰嘛然後我爸是那個眼睛啊還在跑醫院\n"
    "[15:25:10] 狗與露: 我下禮拜六要去溪頭露營"
)
_SUBJECT_HEARD_BY = ["showay", "狗與露"]


def test_validate_event_subject_wo_uses_quote_speaker_not_llm_owner():
    raw = {"owner": "showay", "activity": "露營", "subject": "我",
           "quote": "我下禮拜六要去溪頭露營"}
    ev = validate_event(raw, _SUBJECT_HEARD_BY, _SUBJECT_TRANSCRIPT, time.time())
    assert ev is not None
    assert ev["owner"] == "狗與露"


def test_validate_event_subject_family_member_dropped():
    raw = {"owner": "showay", "activity": "看醫生", "subject": "我媽",
           "quote": "我媽是腰嘛然後我爸是那個眼睛啊還在跑醫院"}
    assert validate_event(raw, _SUBJECT_HEARD_BY, _SUBJECT_TRANSCRIPT, time.time()) is None


def test_validate_event_subject_ta_pronoun_dropped():
    raw = {"owner": "狗與露", "activity": "露營", "subject": "他",
           "quote": "我下禮拜六要去溪頭露營"}
    assert validate_event(raw, _SUBJECT_HEARD_BY, _SUBJECT_TRANSCRIPT, time.time()) is None


def test_validate_event_subject_missing_dropped():
    raw = {"owner": "showay", "activity": "露營"}
    assert validate_event(raw, _SUBJECT_HEARD_BY, _SUBJECT_TRANSCRIPT, time.time()) is None


def test_validate_event_subject_wo_quote_not_in_transcript_dropped():
    raw = {"owner": "showay", "activity": "露營", "subject": "我",
           "quote": "完全不相關的句子"}
    assert validate_event(raw, _SUBJECT_HEARD_BY, _SUBJECT_TRANSCRIPT, time.time()) is None


def test_validate_event_subject_speaker_name_with_empty_quote_kept():
    raw = {"owner": "狗與露", "activity": "露營", "subject": "狗與露", "quote": ""}
    ev = validate_event(raw, _SUBJECT_HEARD_BY, _SUBJECT_TRANSCRIPT, time.time())
    assert ev is not None
    assert ev["owner"] == "狗與露"


def test_validate_event_subject_speaker_name_not_in_heard_by_dropped():
    raw = {"owner": "showay", "activity": "露營", "subject": "狗與露", "quote": ""}
    assert validate_event(raw, ["showay"], _SUBJECT_TRANSCRIPT, time.time()) is None


def test_validate_event_subject_marvin_dropped():
    raw = {"owner": "showay", "activity": "露營", "subject": "馬文", "quote": ""}
    assert validate_event(raw, _SUBJECT_HEARD_BY, _SUBJECT_TRANSCRIPT, time.time()) is None


def test_validate_event_subject_wo_men_quote_substring_of_speaker_line():
    raw = {"owner": "狗與露", "activity": "看醫生", "subject": "我們",
           "quote": "我媽是腰嘛然後我爸是那個眼睛啊還在跑醫院"}
    ev = validate_event(raw, _SUBJECT_HEARD_BY, _SUBJECT_TRANSCRIPT, time.time())
    assert ev is not None
    assert ev["owner"] == "showay"


def test_validate_event_subject_wo_quote_speaker_not_in_heard_by_dropped():
    raw = {"owner": "showay", "activity": "露營", "subject": "我",
           "quote": "我下禮拜六要去溪頭露營"}
    assert validate_event(raw, ["showay"], _SUBJECT_TRANSCRIPT, time.time()) is None


@pytest.mark.parametrize(
    "quote,expected",
    [
        ("我下禮拜六要去溪頭露營", "狗與露"),
        ("", None),
        ("完全不相關的句子", None),
    ],
)
def test_quote_speaker(quote, expected):
    assert _quote_speaker(quote, _SUBJECT_TRANSCRIPT) == expected


def test_extract_system_prompt_has_subject_field_not_old_owner_wording():
    assert '"subject":""' in EXTRACT_SYSTEM_PROMPT
    assert "必須是逐字稿中出現的說話者名稱" not in EXTRACT_SYSTEM_PROMPT
    assert "陪家人或朋友看病住院" in EXTRACT_SYSTEM_PROMPT


# ═══════════════════════════════════════════════════════
# find_merge_target
# ═══════════════════════════════════════════════════════

def _recent_row(id_, owner="showay", activity="露營", event_date=None, last_seen=0.0, place=None):
    return {"id": id_, "owner": owner, "activity": activity, "event_date": event_date,
            "last_seen": last_seen, "heard_by": [], "participants": [], "quotes": [],
            "date_text": "", "place": place, "first_seen": last_seen, "created_at": last_seen,
            "guild_id": 1}


def test_find_merge_target_same_as_valid():
    recent = [_recent_row(5, activity="完全不像")]
    ev = {"owner": "showay", "activity": "露營", "event_date": None, "same_as": 5}
    assert find_merge_target(ev, recent, time.time()) == 5


# ═══════════════════════════════════════════════════════
# validate_event — 後續關心（你/他）併回既有事件（拼圖制）
# ═══════════════════════════════════════════════════════

_FOLLOWUP_TRANSCRIPT = (
    "[15:40:52] showay: 你離開醫院了嗎\n"
    "[15:41:20] 狗與露: 因為可能病房沒辦法做那麼多人吧"
)
_FOLLOWUP_HEARD_BY = ["showay", "狗與露", "weakgogo"]


def test_validate_event_followup_subject_ni_merges_into_recent_owner():
    recent = [_recent_row(5, owner="狗與露")]
    raw = {"owner": "showay", "activity": "醫院", "subject": "你", "same_as": 5}
    ev = validate_event(raw, _FOLLOWUP_HEARD_BY, _FOLLOWUP_TRANSCRIPT, time.time(), None, recent)
    assert ev is not None
    assert ev["owner"] == "狗與露"
    assert ev["update_only"] is True
    assert ev["same_as"] == 5


def test_validate_event_followup_subject_ta_merges_into_recent_owner():
    recent = [_recent_row(5, owner="狗與露")]
    raw = {"owner": "showay", "activity": "醫院", "subject": "他", "same_as": 5}
    ev = validate_event(raw, _FOLLOWUP_HEARD_BY, _FOLLOWUP_TRANSCRIPT, time.time(), None, recent)
    assert ev is not None
    assert ev["owner"] == "狗與露"
    assert ev["update_only"] is True


def test_validate_event_followup_same_as_not_in_recent_dropped():
    recent = [_recent_row(5, owner="狗與露")]
    raw = {"owner": "showay", "activity": "醫院", "subject": "你", "same_as": 99}
    assert validate_event(raw, _FOLLOWUP_HEARD_BY, _FOLLOWUP_TRANSCRIPT, time.time(), None, recent) is None


def test_validate_event_followup_target_owner_not_present_dropped():
    recent = [_recent_row(5, owner="狗與露")]
    raw = {"owner": "showay", "activity": "醫院", "subject": "你", "same_as": 5}
    heard_by = ["showay", "weakgogo"]
    assert validate_event(raw, heard_by, _FOLLOWUP_TRANSCRIPT, time.time(), None, recent) is None


def test_validate_event_followup_self_subject_without_same_as_not_update_only():
    raw = {"owner": "showay", "activity": "醫院", "subject": "我", "same_as": None,
           "quote": "你離開醫院了嗎"}
    ev = validate_event(raw, _FOLLOWUP_HEARD_BY, _FOLLOWUP_TRANSCRIPT, time.time(), None, None)
    assert ev is not None
    assert ev["owner"] == "showay"
    assert ev["update_only"] is False


def test_validate_event_followup_no_recent_arg_dropped():
    raw = {"owner": "showay", "activity": "醫院", "subject": "你", "same_as": 5}
    assert validate_event(raw, _FOLLOWUP_HEARD_BY, _FOLLOWUP_TRANSCRIPT, time.time(), None, None) is None


def test_find_merge_target_same_as_other_owner_falls_back():
    recent = [_recent_row(5, owner="狗與露")]
    ev = {"owner": "showay", "activity": "露營", "event_date": None, "same_as": 5}
    assert find_merge_target(ev, recent, time.time()) is None


def test_find_merge_target_pinyin_match_date_within_tolerance_merges():
    day = 86400.0
    recent = [_recent_row(1, activity="路營", event_date=1000.0)]
    ev = {"owner": "showay", "activity": "露營", "event_date": 1000.0 + day, "same_as": None}
    assert find_merge_target(ev, recent, time.time()) == 1


def test_find_merge_target_date_diff_too_large_no_merge():
    day = 86400.0
    recent = [_recent_row(1, activity="路營", event_date=1000.0)]
    ev = {"owner": "showay", "activity": "露營", "event_date": 1000.0 + 2 * day, "same_as": None}
    assert find_merge_target(ev, recent, time.time()) is None


def test_find_merge_target_different_activity_no_merge():
    recent = [_recent_row(1, activity="看醫生", event_date=1000.0)]
    ev = {"owner": "showay", "activity": "露營", "event_date": 1000.0, "same_as": None}
    assert find_merge_target(ev, recent, time.time()) is None


def test_find_merge_target_null_dates_within_window_merges():
    window_end = 10000.0
    recent = [_recent_row(1, activity="露營", event_date=None, last_seen=window_end - 2 * 3600)]
    ev = {"owner": "showay", "activity": "露營", "event_date": None, "same_as": None}
    assert find_merge_target(ev, recent, window_end) == 1


def test_find_merge_target_null_dates_outside_window_no_merge():
    window_end = 10000.0
    recent = [_recent_row(1, activity="露營", event_date=None, last_seen=window_end - 4 * 3600)]
    ev = {"owner": "showay", "activity": "露營", "event_date": None, "same_as": None}
    assert find_merge_target(ev, recent, window_end) is None


def test_find_merge_target_different_owner_none():
    recent = [_recent_row(1, owner="狗與露", activity="露營", event_date=1000.0)]
    ev = {"owner": "showay", "activity": "露營", "event_date": 1000.0, "same_as": None}
    assert find_merge_target(ev, recent, time.time()) is None


# ═══════════════════════════════════════════════════════
# visible_events / parse_ids / render_answer
# ═══════════════════════════════════════════════════════

def test_visible_events_subset_kept():
    events = [{"heard_by": ["showay", "狗與露"]}]
    assert visible_events(events, ["showay"]) == events


def test_visible_events_missing_person_filtered():
    events = [{"heard_by": ["showay"]}]
    assert visible_events(events, ["showay", "狗與露"]) == []


def test_visible_events_empty_present_returns_empty():
    events = [{"heard_by": ["showay"]}]
    assert visible_events(events, []) == []


def test_parse_ids_drops_non_candidates():
    assert parse_ids('{"ids":[1,2,3]}', {1, 3}) == [1, 3]


def test_parse_ids_string_number_converted():
    assert parse_ids('{"ids":["2"]}', {2}) == [2]


def test_parse_ids_bool_dropped():
    assert parse_ids('{"ids":[true, 1]}', {1}) == [1]


def test_parse_ids_bad_json_returns_empty():
    assert parse_ids("not json", {1}) == []


def test_parse_ids_dedup_preserves_order():
    assert parse_ids('{"ids":[1,1,2]}', {1, 2}) == [1, 2]


def test_render_answer_self_uses_ni():
    ev = {"owner": "showay", "activity": "露營", "place": None, "event_date": None,
          "quotes": [], "first_seen": time.time()}
    text = render_answer(ev, "showay")
    assert text.startswith("我記得你")


def test_render_answer_other_uses_name():
    ev = {"owner": "showay", "activity": "露營", "place": None, "event_date": None,
          "quotes": [], "first_seen": time.time()}
    text = render_answer(ev, "狗與露")
    assert "showay" in text


def test_render_answer_never_speaks_place():
    """2026-10-10 重播：「我還在臺中，明天早上走」被存成 place=臺中（出發地當目的地）。
    地名出現在逐字稿 ≠ 意思對，所以回答一律不念地點（place 仍存著供選事件用）。"""
    ev = {"owner": "showay", "activity": "露營", "place": "臺中", "event_date": None,
          "quotes": [], "first_seen": time.time()}
    text = render_answer(ev, "showay")
    assert "臺中" not in text and "地點" not in text


def test_render_answer_no_date_text_omitted():
    ev = {"owner": "showay", "activity": "露營", "place": "溪頭", "event_date": None,
          "quotes": [], "first_seen": time.time()}
    assert "日期是" not in render_answer(ev, "showay")


def test_render_answer_quote_truncated_over_30_chars():
    long_quote = "這是一句超過三十個字的引述句用來測試截斷邏輯會不會正確地加上刪節號喔好長好長"
    assert len(long_quote) > 30
    ev = {"owner": "showay", "activity": "露營", "place": None, "event_date": None,
          "quotes": [long_quote], "first_seen": time.time()}
    text = render_answer(ev, "showay")
    assert "…" in text
    assert long_quote not in text


def test_render_answer_no_quotes_omits_said_that():
    ev = {"owner": "showay", "activity": "露營", "place": None, "event_date": None,
          "quotes": [], "first_seen": time.time()}
    assert "那時說" not in render_answer(ev, "showay")


# ═══════════════════════════════════════════════════════
# extract_and_store
# ═══════════════════════════════════════════════════════

def _router_with(contents):
    router = MagicMock()
    router._call_llm = AsyncMock(side_effect=contents)
    return router


def _events_json(events):
    return json.dumps({"events": events}, ensure_ascii=False)


@pytest.mark.asyncio
async def test_extract_and_store_calls_llm_with_correct_kwargs():
    store = EventStore(":memory:")
    router = _router_with([_events_json([])])
    await extract_and_store(router, store, 1, "逐字稿", ["showay"], time.time())
    router._call_llm.assert_awaited_once()
    kwargs = router._call_llm.call_args.kwargs
    assert kwargs["purpose"] == "extract_events"
    assert kwargs["prefer_providers"] == ("gemini_free", "groq")
    assert kwargs["is_json"] is True


@pytest.mark.asyncio
async def test_extract_and_store_three_windows_merge_into_one():
    store = EventStore(":memory:")
    transcript = "showay 說下禮拜六要去溪頭露營，引述句一。路營也是這句。"
    window_end = time.time()

    contents = [
        _events_json([{"owner": "showay", "activity": "露營", "place": None, "date": None,
                       "date_text": "", "participants": [], "quote": "", "same_as": None,
                       "subject": "showay"}]),
        _events_json([{"owner": "showay", "activity": "路營", "place": None, "date": None,
                       "date_text": "", "participants": [], "quote": "", "same_as": None,
                       "subject": "showay"}]),
        _events_json([{"owner": "showay", "activity": "露營", "place": None, "date": None,
                       "date_text": "", "participants": [], "quote": "", "same_as": None,
                       "subject": "showay"}]),
    ]
    router = _router_with(contents)

    await extract_and_store(router, store, 1, transcript, ["showay"], window_end)
    await extract_and_store(router, store, 1, transcript, ["狗與露"], window_end + 300)
    await extract_and_store(router, store, 1, transcript, ["showay", "狗與露"], window_end + 600)

    events = store.list_guild(1, 0)
    assert len(events) == 1
    assert sorted(events[0]["heard_by"]) == ["showay", "狗與露"]


@pytest.mark.asyncio
async def test_extract_and_store_same_as_merges_despite_different_activity():
    store = EventStore(":memory:")
    transcript = "showay 說要去溪頭露營，引述句一。去玩也是這句。"
    window_end = time.time()
    first = _events_json([{"owner": "showay", "activity": "露營", "place": None, "date": None,
                           "date_text": "", "participants": [], "quote": "", "same_as": None,
                           "subject": "showay"}])
    router1 = _router_with([first])
    await extract_and_store(router1, store, 1, transcript, ["showay"], window_end)
    existing_id = store.list_guild(1, 0)[0]["id"]

    second = _events_json([{"owner": "showay", "activity": "去玩", "place": None, "date": None,
                            "date_text": "", "participants": [], "quote": "", "same_as": existing_id,
                            "subject": "showay"}])
    router2 = _router_with([second])
    await extract_and_store(router2, store, 1, transcript, ["showay"], window_end + 300)

    events = store.list_guild(1, 0)
    assert len(events) == 1


@pytest.mark.asyncio
async def test_extract_and_store_two_events_same_window_merge_via_recent_insert():
    store = EventStore(":memory:")
    transcript = "showay 說要去溪頭露營，引述句一。路營也是這句。"
    window_end = time.time()
    content = _events_json([
        {"owner": "showay", "activity": "露營", "place": None, "date": None,
         "date_text": "", "participants": [], "quote": "", "same_as": None, "subject": "showay"},
        {"owner": "showay", "activity": "路營", "place": None, "date": None,
         "date_text": "", "participants": [], "quote": "", "same_as": None, "subject": "showay"},
    ])
    router = _router_with([content])
    stats = await extract_and_store(router, store, 1, transcript, ["showay"], window_end)
    assert stats["added"] == 1
    assert stats["merged"] == 1
    assert len(store.list_guild(1, 0)) == 1


@pytest.mark.asyncio
async def test_extract_and_store_invalid_owner_dropped():
    store = EventStore(":memory:")
    content = _events_json([{"owner": "狗與露", "activity": "露營", "place": None, "date": None,
                             "date_text": "", "participants": [], "quote": "", "same_as": None,
                             "subject": "狗與露"}])
    router = _router_with([content])
    stats = await extract_and_store(router, store, 1, "逐字稿", ["showay"], time.time())
    assert stats["dropped"] == 1
    assert store.list_guild(1, 0) == []


@pytest.mark.asyncio
async def test_extract_and_store_followup_subject_merges_into_existing_event():
    store = EventStore(":memory:")
    window_end = time.time()
    first = _events_json([{"owner": "狗與露", "activity": "住院", "place": None, "date": None,
                           "date_text": "", "participants": [], "quote": "", "same_as": None,
                           "subject": "狗與露"}])
    router1 = _router_with([first])
    await extract_and_store(router1, store, 1, "逐字稿", ["狗與露"], window_end)
    existing_id = store.list_guild(1, 0)[0]["id"]

    transcript = "[15:40:52] showay: 你離開醫院了嗎"
    second = _events_json([{"owner": "showay", "activity": "醫院", "place": None, "date": None,
                            "date_text": "", "participants": [], "quote": "你離開醫院了嗎",
                            "same_as": existing_id, "subject": "你"}])
    router2 = _router_with([second])
    stats = await extract_and_store(router2, store, 1, transcript, ["showay", "狗與露"], window_end + 300)

    assert stats["merged"] == 1
    assert stats["added"] == 0
    events = store.list_guild(1, 0)
    assert len(events) == 1
    assert events[0]["owner"] == "狗與露"
    assert "你離開醫院了嗎" in events[0]["quotes"]


@pytest.mark.asyncio
async def test_extract_and_store_followup_same_as_not_in_recent_dropped_not_added():
    store = EventStore(":memory:")
    window_end = time.time()
    content = _events_json([{"owner": "showay", "activity": "醫院", "place": None, "date": None,
                             "date_text": "", "participants": [], "quote": "",
                             "same_as": 999, "subject": "你"}])
    router = _router_with([content])
    stats = await extract_and_store(router, store, 1, "逐字稿", ["showay"], window_end)

    assert stats["added"] == 0
    assert stats["dropped"] == 1
    assert store.list_guild(1, 0) == []


@pytest.mark.asyncio
async def test_extract_and_store_stats_counts_correct():
    store = EventStore(":memory:")
    content = _events_json([
        {"owner": "showay", "activity": "露營", "place": None, "date": None,
         "date_text": "", "participants": [], "quote": "", "same_as": None, "subject": "showay"},
        {"owner": "狗與露", "activity": "露營", "place": None, "date": None,
         "date_text": "", "participants": [], "quote": "", "same_as": None, "subject": "狗與露"},
    ])
    router = _router_with([content])
    stats = await extract_and_store(router, store, 1, "逐字稿", ["showay"], time.time())
    assert stats == {"added": 1, "merged": 0, "dropped": 1, "hometowns": 0}


# ── 缺口 1（2026-10-10 重播 9/29）：同一趟行程活動名不同（去臺東 vs 回老家）但同地同日 → 合併 ──

def test_find_merge_target_same_place_same_day_different_activity_merges():
    recent = [_recent_row(1, activity="去臺東", event_date=1000.0, place="臺東")]
    ev = {"owner": "showay", "activity": "回老家", "event_date": 1000.0, "place": "台東", "same_as": None}
    assert find_merge_target(ev, recent, time.time()) == 1


def test_find_merge_target_same_place_date_diff_too_large_no_merge():
    day = 86400.0
    recent = [_recent_row(1, activity="去臺東", event_date=1000.0, place="臺東")]
    ev = {"owner": "showay", "activity": "回老家", "event_date": 1000.0 + 2 * day, "place": "臺東", "same_as": None}
    assert find_merge_target(ev, recent, time.time()) is None


def test_find_merge_target_same_place_null_dates_within_window_merges():
    window_end = 10000.0
    recent = [_recent_row(1, activity="去臺東", event_date=None, last_seen=window_end - 3600, place="臺東")]
    ev = {"owner": "showay", "activity": "回老家", "event_date": None, "place": "臺東", "same_as": None}
    assert find_merge_target(ev, recent, window_end) == 1


def test_find_merge_target_different_place_different_activity_no_merge():
    recent = [_recent_row(1, activity="去臺東", event_date=1000.0, place="臺東")]
    ev = {"owner": "showay", "activity": "跑恆春", "event_date": 1000.0, "place": "恆春", "same_as": None}
    assert find_merge_target(ev, recent, time.time()) is None


def test_find_merge_target_one_place_missing_no_place_merge():
    recent = [_recent_row(1, activity="去臺東", event_date=1000.0, place="臺東")]
    ev = {"owner": "showay", "activity": "回老家", "event_date": 1000.0, "place": None, "same_as": None}
    assert find_merge_target(ev, recent, time.time()) is None


# ═══════════════════════════════════════════════════════
# hometowns — set/get/prune
# ═══════════════════════════════════════════════════════

def test_set_then_get_hometown_roundtrip():
    store = EventStore(":memory:")
    now = time.time()
    store.set_hometown(1, "showay", "臺東", "我老家在臺東", now)
    assert store.get_hometowns(1, ["showay"]) == {"showay": "臺東"}


def test_set_hometown_overwrites_with_latest():
    store = EventStore(":memory:")
    now = time.time()
    store.set_hometown(1, "showay", "臺東", "我老家在臺東", now)
    store.set_hometown(1, "showay", "高雄", "我老家其實在高雄", now + 10)
    assert store.get_hometowns(1, ["showay"]) == {"showay": "高雄"}


def test_get_hometowns_empty_persons_returns_empty_without_query():
    store = EventStore(":memory:")
    store.set_hometown(1, "showay", "臺東", "我老家在臺東", time.time())
    assert store.get_hometowns(1, []) == {}


def test_get_hometowns_only_returns_requested_persons():
    store = EventStore(":memory:")
    now = time.time()
    store.set_hometown(1, "showay", "臺東", "我老家在臺東", now)
    store.set_hometown(1, "狗與露", "宜蘭", "我老家在宜蘭", now)
    assert store.get_hometowns(1, ["showay"]) == {"showay": "臺東"}


def test_prune_also_deletes_expired_hometowns_return_value_is_events_count():
    store = EventStore(":memory:")
    now = time.time()
    store.add(1, _ev(), ["showay"], now - 31 * 86400)
    store.add(1, _ev(), ["showay"], now - 1 * 86400)
    store.set_hometown(1, "showay", "臺東", "我老家在臺東", now - 31 * 86400)
    store.set_hometown(1, "狗與露", "宜蘭", "我老家在宜蘭", now - 1 * 86400)
    deleted = store.prune(retention_days=30, now=now)
    assert deleted == 1  # 仍是 events 刪除筆數
    assert store.get_hometowns(1, ["showay", "狗與露"]) == {"狗與露": "宜蘭"}


# ═══════════════════════════════════════════════════════
# parse_hometowns
# ═══════════════════════════════════════════════════════

def test_parse_hometowns_plain_json():
    assert parse_hometowns('{"hometowns":[{"person":"showay"}]}') == [{"person": "showay"}]


def test_parse_hometowns_code_fence():
    content = '```json\n{"hometowns":[{"person":"showay"}]}\n```'
    assert parse_hometowns(content) == [{"person": "showay"}]


def test_parse_hometowns_bad_json_returns_empty():
    assert parse_hometowns("not json") == []


def test_parse_hometowns_missing_key_returns_empty():
    assert parse_hometowns('{"events":[]}') == []


# ═══════════════════════════════════════════════════════
# validate_hometown
# ═══════════════════════════════════════════════════════

_HOME_TRANSCRIPT = "showay 說：我老家在臺東，過年都會回去，引述句：我老家在臺東。下禮拜我要去臺東玩"


@pytest.mark.parametrize(
    "raw,heard_by,expected_none",
    [
        ({"person": "showay", "place": "臺東", "quote": "我老家在臺東"}, ["showay"], False),
        ({"person": "狗與露", "place": "臺東", "quote": "我老家在臺東"}, ["showay"], True),  # 不在 heard_by
        ({"person": "馬文", "place": "臺東", "quote": "我老家在臺東"}, ["馬文"], True),  # 馬文
        ({"person": "showay", "place": "臺東", "quote": "完全不相關的句子"}, ["showay"], True),  # quote 不在逐字稿
        ({"person": "showay", "place": "臺東", "quote": "我要去臺東玩"}, ["showay"], True),  # 原話在逐字稿、含地名，但無老家/家鄉/故鄉
        ({"person": "showay", "place": "高雄", "quote": "我老家在臺東"}, ["showay"], True),  # place 不在 quote 內
    ],
)
def test_validate_hometown_parametrized(raw, heard_by, expected_none):
    result = validate_hometown(raw, heard_by, _HOME_TRANSCRIPT)
    assert (result is None) == expected_none


def test_validate_hometown_valid_returns_dict():
    raw = {"person": "showay", "place": "臺東", "quote": "我老家在臺東"}
    result = validate_hometown(raw, ["showay"], _HOME_TRANSCRIPT)
    assert result == {"person": "showay", "place": "臺東", "quote": "我老家在臺東"}


# ═══════════════════════════════════════════════════════
# validate_event with hometowns background
# ═══════════════════════════════════════════════════════

def test_validate_event_place_not_in_transcript_but_matches_hometown_kept():
    raw = {"owner": "showay", "activity": "回老家", "place": "臺東", "subject": "showay"}
    ev = validate_event(raw, ["showay"], "showay 說要回老家了", time.time(), {"showay": "台東"})
    assert ev["place"] == "臺東"


def test_validate_event_place_not_in_transcript_and_not_hometown_is_none():
    raw = {"owner": "showay", "activity": "回老家", "place": "臺東", "subject": "showay"}
    ev = validate_event(raw, ["showay"], "showay 說要回老家了", time.time(), {"showay": "高雄"})
    assert ev["place"] is None


def test_validate_event_hometowns_none_behaves_like_before():
    raw = {"owner": "showay", "activity": "回老家", "place": "臺東", "subject": "showay"}
    ev = validate_event(raw, ["showay"], "showay 說要回老家了", time.time(), None)
    assert ev["place"] is None


# ═══════════════════════════════════════════════════════
# build_extract_user_prompt with hometowns
# ═══════════════════════════════════════════════════════

def test_build_extract_user_prompt_with_hometowns_includes_background_line():
    prompt = build_extract_user_prompt("逐字稿", [], time.time(), {"showay": "臺東"})
    assert "showay 老家在臺東" in prompt


def test_build_extract_user_prompt_without_hometowns_includes_placeholder():
    prompt = build_extract_user_prompt("逐字稿", [], time.time())
    assert "背景：\n（無）" in prompt


# ═══════════════════════════════════════════════════════
# extract_and_store — hometown learning + background usage
# ═══════════════════════════════════════════════════════

def _mixed_json(events, hometowns):
    return json.dumps({"events": events, "hometowns": hometowns}, ensure_ascii=False)


@pytest.mark.asyncio
async def test_extract_and_store_learns_hometown_and_uses_it_for_place():
    store = EventStore(":memory:")
    transcript = "showay 說：我老家在臺東，過年要回去。引述句：我老家在臺東"
    window_end = time.time()
    content = _mixed_json(
        events=[{"owner": "showay", "activity": "回老家", "place": "臺東", "date": None,
                 "date_text": "", "participants": [], "quote": "", "same_as": None,
                 "subject": "showay"}],
        hometowns=[{"person": "showay", "place": "臺東", "quote": "我老家在臺東"}],
    )
    router = _router_with([content])
    stats = await extract_and_store(router, store, 1, transcript, ["showay"], window_end)

    assert stats["hometowns"] == 1
    assert store.get_hometowns(1, ["showay"]) == {"showay": "臺東"}
    events = store.list_guild(1, 0)
    assert len(events) == 1
    assert events[0]["place"] == "臺東"


@pytest.mark.asyncio
async def test_extract_and_store_second_window_uses_known_hometown_and_merges():
    store = EventStore(":memory:")
    transcript1 = "showay 說：我老家在臺東，過年要回去。引述句：我老家在臺東"
    window_end = time.time()
    content1 = _mixed_json(
        events=[{"owner": "showay", "activity": "回老家", "place": "臺東", "date": None,
                 "date_text": "", "participants": [], "quote": "", "same_as": None,
                 "subject": "showay"}],
        hometowns=[{"person": "showay", "place": "臺東", "quote": "我老家在臺東"}],
    )
    router1 = _router_with([content1])
    await extract_and_store(router1, store, 1, transcript1, ["showay"], window_end)

    transcript2 = "showay 說晚點要回老家了"
    content2 = _mixed_json(
        events=[{"owner": "showay", "activity": "回老家", "place": "臺東", "date": None,
                 "date_text": "", "participants": [], "quote": "", "same_as": None,
                 "subject": "showay"}],
        hometowns=[],
    )
    router2 = _router_with([content2])
    stats2 = await extract_and_store(router2, store, 1, transcript2, ["showay"], window_end + 1000)

    assert stats2["merged"] == 1
    events = store.list_guild(1, 0)
    assert len(events) == 1
    assert events[0]["place"] == "臺東"

    user_prompt = router2._call_llm.call_args.args[1]
    assert "showay 老家在臺東" in user_prompt


@pytest.mark.asyncio
async def test_extract_and_store_hometown_learned_this_window_validates_event_place():
    """同窗剛學到的老家要立刻能放行本窗事件的地點：逐字稿寫「台東」，事件 place 寫「臺東」
    （不是逐字稿子字串），只能靠剛學到的 showay 老家=台東 通過。"""
    store = EventStore(":memory:")
    transcript = "showay 說：我老家在台東，這週末回老家"
    content = _mixed_json(
        events=[{"owner": "showay", "activity": "回老家", "place": "臺東", "date": None,
                 "date_text": "", "participants": [], "quote": "", "same_as": None,
                 "subject": "showay"}],
        hometowns=[{"person": "showay", "place": "台東", "quote": "我老家在台東"}],
    )
    router = _router_with([content])
    await extract_and_store(router, store, 1, transcript, ["showay"], 1791640000.0)
    assert store.list_guild(1, 0)[0]["place"] == "臺東"


# ═══════════════════════════════════════════════════════
# learn_hometowns_local（本地 regex 補漏：LLM 漏抓 9/29 陳進文「我臺東老家」）
# ═══════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "line,expected",
    [
        ("[23:24:39] 陳進文: 所以說我臺東老家那邊我臺東老家不是", [("陳進文", "臺東")]),
        ("[00:24:56] 陳進文: 他也知道有去過我們臺東老家", [("陳進文", "臺東")]),
        ("[10:00:00] showay: 我老家在高雄啦", [("showay", "高雄")]),
        ("[10:00:00] showay: 我家鄉在花蓮", [("showay", "花蓮")]),
        ("[10:00:00] showay: 我們的老家很大", []),
        ("[10:00:00] 狗與露: 但是其實黑老家", []),
        ("[10:00:00] showay: 老家在那邊", []),
        ("[10:00:00] showay: 我要回老家", []),
        ("[10:00:00] 馬文: 我臺東老家", []),
        ("[10:00:00] weakgogo: 我臺東老家", []),  # 不在 heard_by
    ],
)
def test_learn_hometowns_local(line, expected):
    got = learn_hometowns_local(line, ["陳進文", "showay", "狗與露", "馬文"])
    assert [(h["person"], h["place"]) for h in got] == expected


def test_learn_hometowns_local_quote_is_the_line_text():
    got = learn_hometowns_local("[23:24:39] 陳進文: 所以說我臺東老家那邊", ["陳進文"])
    assert got[0]["quote"] == "所以說我臺東老家那邊"


def test_learn_hometowns_local_dedup_same_person_place():
    t = "[23:24:39] 陳進文: 我臺東老家\n[23:25:00] 陳進文: 我們臺東老家那邊"
    assert len(learn_hometowns_local(t, ["陳進文"])) == 1


@pytest.mark.asyncio
async def test_extract_and_store_local_hometown_learned_before_llm_and_in_prompt():
    """LLM 沒回 hometowns 也要學到；且在 LLM 呼叫前學到，所以本窗 prompt 背景就有。"""
    store = EventStore(":memory:")
    transcript = "[23:24:39] 陳進文: 所以說我臺東老家那邊"
    router = _router_with([_mixed_json(events=[], hometowns=[])])
    stats = await extract_and_store(router, store, 1, transcript, ["陳進文"], 1791640000.0)
    assert store.get_hometowns(1, ["陳進文"]) == {"陳進文": "臺東"}
    assert stats["hometowns"] == 1
    user_prompt = router._call_llm.call_args.args[1]
    assert "陳進文 老家在臺東" in user_prompt
