"""Unit tests for theme_link.py — 主題連結 MVP 的純函式層。"""
from __future__ import annotations

from dataclasses import dataclass

from theme_link import (
    collect_tag_occurrences,
    eligible_tags,
    merge_theme_picks_into_local,
    normalize_tags,
    parse_room_veto,
    parse_song_card,
    parse_theme_meaning,
    parse_theme_picks,
    parse_tpe_timestamp,
    presence_gate_open,
    qualifying_theme_tags,
    reconstruct_present_users,
    select_card_queue,
    select_match_candidates,
)


# ── 驗收 1：正規化 ───────────────────────────────────────────────────────────

def test_normalize_tags_splits_on_delimiters():
    assert normalize_tags("搬家、延期") == ["搬家", "延期"]


def test_normalize_tags_strips_internal_whitespace():
    assert normalize_tags("AI 剪輯") == ["AI剪輯"]


def test_normalize_tags_blank_value_is_empty():
    assert normalize_tags("-") == []


def test_normalize_tags_drops_short_tags():
    assert normalize_tags("A、搬家") == ["搬家"]


def test_normalize_tags_empty_input():
    assert normalize_tags("") == []


# ── 驗收 2：合格 tag ─────────────────────────────────────────────────────────

@dataclass
class _FakeEntry:
    ts_str: str
    meme_id: str


def test_eligible_tags_same_day_three_times_is_not_eligible():
    now = parse_tpe_timestamp("2026-10-27 12:00:00")
    entries = [
        _FakeEntry("2026-10-20 10:00:00", "搬家"),
        _FakeEntry("2026-10-20 11:00:00", "搬家"),
        _FakeEntry("2026-10-20 12:00:00", "搬家"),
    ]
    occ = collect_tag_occurrences(entries, now)
    assert "搬家" not in eligible_tags(occ)


def test_eligible_tags_two_different_dates_is_eligible():
    now = parse_tpe_timestamp("2026-10-27 12:00:00")
    entries = [
        _FakeEntry("2026-10-20 10:00:00", "搬家"),
        _FakeEntry("2026-10-21 10:00:00", "搬家"),
    ]
    occ = collect_tag_occurrences(entries, now)
    assert "搬家" in eligible_tags(occ)


def test_eligible_tags_excludes_entries_older_than_14_days():
    now = parse_tpe_timestamp("2026-10-27 12:00:00")
    entries = [
        _FakeEntry("2026-10-01 10:00:00", "搬家"),  # 超過 14 天
        _FakeEntry("2026-10-21 10:00:00", "搬家"),
    ]
    occ = collect_tag_occurrences(entries, now)
    # 只剩一個合格日期 → 不合格
    assert "搬家" not in eligible_tags(occ)


# ── 驗收 3：heard_by ─────────────────────────────────────────────────────────

def test_heard_by_join_before_leave_after_window_is_present():
    events = [
        {"ts": 100.0, "user_name": "showay", "event": "join", "is_bot": False},
        {"ts": 500.0, "user_name": "showay", "event": "leave", "is_bot": False},
    ]
    present = reconstruct_present_users(events, window_start=200.0, window_end=300.0)
    assert "showay" in present


def test_heard_by_leave_before_window_start_is_not_present():
    events = [
        {"ts": 50.0, "user_name": "showay", "event": "join", "is_bot": False},
        {"ts": 100.0, "user_name": "showay", "event": "leave", "is_bot": False},
    ]
    present = reconstruct_present_users(events, window_start=200.0, window_end=300.0)
    assert "showay" not in present


def test_heard_by_join_inside_window_is_present():
    events = [
        {"ts": 250.0, "user_name": "showay", "event": "join", "is_bot": False},
    ]
    present = reconstruct_present_users(events, window_start=200.0, window_end=300.0)
    assert "showay" in present


def test_heard_by_bot_never_counted():
    events = [
        {"ts": 100.0, "user_name": "Marvin", "event": "join", "is_bot": True},
    ]
    present = reconstruct_present_users(events, window_start=50.0, window_end=300.0)
    assert "Marvin" not in present


# ── 驗收 4：在場閘門 ─────────────────────────────────────────────────────────

def test_presence_gate_open_when_subset():
    assert presence_gate_open(["showay"], {"showay", "狗與露"}) is True


def test_presence_gate_closed_when_member_not_in_heard_by():
    assert presence_gate_open(["showay", "大肚"], {"showay"}) is False


def test_presence_gate_closed_when_members_empty():
    assert presence_gate_open([], {"showay"}) is False


# ── 驗收 5：批次解析 ─────────────────────────────────────────────────────────

def test_theme_picks_drops_out_of_range_index():
    raw = '{"picks": [{"index": 0, "layer": "意象"}, {"index": 9, "layer": "深層"}]}'
    picks = parse_theme_picks(raw, n_cards=3)
    assert picks == [(0, "意象")]


def test_theme_meaning_sensitive_missing_defaults_true():
    raw = '{"meaning": "旅行：離開熟悉的地方"}'
    result = parse_theme_meaning(raw)
    assert result["sensitive"] is True


def test_theme_meaning_sensitive_wrong_type_defaults_true():
    raw = '{"meaning": "旅行", "sensitive": "false"}'
    result = parse_theme_meaning(raw)
    assert result["sensitive"] is True


def test_theme_meaning_sensitive_bool_false_is_kept():
    raw = '{"meaning": "旅行", "sensitive": false}'
    result = parse_theme_meaning(raw)
    assert result["sensitive"] is False


def test_song_card_parse_failure_gives_low_familiarity():
    result = parse_song_card("這不是 JSON", now=1000.0)
    assert result["familiarity"] == "低"
    assert result["deep_story"] == ""


def test_song_card_unsure_deep_story_gives_low_familiarity():
    raw = '{"imagery": "海邊", "deep_story": "不確定", "weight": "輕", "familiarity": "高"}'
    result = parse_song_card(raw, now=1000.0)
    assert result["familiarity"] == "低"
    assert result["deep_story"] == ""


def test_song_card_valid_parses_through():
    raw = ('{"imagery": "海邊、離別", "deep_story": "寫給遠行的朋友", '
           '"relation": "朋友", "weight": "中", "familiarity": "高"}')
    result = parse_song_card(raw, now=1000.0)
    assert result["deep_story"] == "寫給遠行的朋友"
    assert result["familiarity"] == "高"
    assert result["created_at"] == 1000.0


def test_room_veto_parse_failure_defaults_true():
    assert parse_room_veto("不是 JSON") is True


def test_room_veto_missing_field_defaults_true():
    assert parse_room_veto("{}") is True


def test_room_veto_valid_false():
    assert parse_room_veto('{"touches_sensitive": false}') is False


def test_select_match_candidates_empty_when_no_eligible_cards():
    cards = [
        {"video_id": "a", "familiarity": "低", "weight": "輕", "total_plays": 10},
        {"video_id": "b", "familiarity": "高", "weight": "重", "total_plays": 20},
    ]
    assert select_match_candidates(cards) == []


def test_select_match_candidates_sorts_by_total_plays():
    cards = [
        {"video_id": "a", "familiarity": "高", "weight": "輕", "total_plays": 5},
        {"video_id": "b", "familiarity": "中", "weight": "中", "total_plays": 50},
    ]
    result = select_match_candidates(cards)
    assert [c["video_id"] for c in result] == ["b", "a"]


def test_select_card_queue_skips_existing_and_low_play_count():
    songs = {
        "k1": {"title": "歌A", "total_plays": 1, "webpage_url": "https://www.youtube.com/watch?v=AAAAAAAAAAA"},
        "k2": {"title": "歌B", "total_plays": 5, "webpage_url": "https://www.youtube.com/watch?v=BBBBBBBBBBB"},
        "k3": {"title": "歌C", "total_plays": 9, "webpage_url": "https://www.youtube.com/watch?v=CCCCCCCCCCC"},
    }
    result = select_card_queue(songs, existing_video_ids={"CCCCCCCCCCC"})
    assert [vid for vid, _ in result] == ["BBBBBBBBBBB"]


# ── 執行時合格 tag（驗收 8 的純函式部分）────────────────────────────────────

def test_qualifying_theme_tags_filters_sensitive_and_vetoed_and_absent():
    now = 100000.0
    rows = [
        {"tag": "敏感話題", "meaning": "x", "sensitive": True, "room_veto": None,
         "heard_by": ["showay"], "last_seen": now - 1000},
        {"tag": "被房間擋", "meaning": "x", "sensitive": False, "room_veto": True,
         "heard_by": ["showay"], "last_seen": now - 1000},
        {"tag": "有人不在場", "meaning": "x", "sensitive": False, "room_veto": None,
         "heard_by": ["showay"], "last_seen": now - 1000},
        {"tag": "合格", "meaning": "y", "sensitive": False, "room_veto": None,
         "heard_by": ["showay", "大肚"], "last_seen": now - 500},
    ]
    result = qualifying_theme_tags(rows, members=["showay", "大肚"], now=now,
                                    used_tags_14d=set(), used_tag_today=False)
    assert result == [("合格", "y")]


def test_qualifying_theme_tags_empty_members_gives_nothing():
    rows = [{"tag": "合格", "meaning": "y", "sensitive": False, "room_veto": None,
             "heard_by": [], "last_seen": 100.0}]
    assert qualifying_theme_tags(rows, members=[], now=100.0,
                                  used_tags_14d=set(), used_tag_today=False) == []


def test_qualifying_theme_tags_used_today_gives_nothing():
    rows = [{"tag": "合格", "meaning": "y", "sensitive": False, "room_veto": None,
             "heard_by": ["showay"], "last_seen": 100.0}]
    assert qualifying_theme_tags(rows, members=["showay"], now=100.0,
                                  used_tags_14d=set(), used_tag_today=True) == []


def test_qualifying_theme_tags_used_within_14d_excluded():
    rows = [{"tag": "用過", "meaning": "y", "sensitive": False, "room_veto": None,
             "heard_by": ["showay"], "last_seen": 100.0}]
    assert qualifying_theme_tags(rows, members=["showay"], now=100.0,
                                  used_tags_14d={"用過"}, used_tag_today=False) == []


def test_qualifying_theme_tags_sorted_newest_first_and_capped_at_three():
    rows = [
        {"tag": f"t{i}", "meaning": "y", "sensitive": False, "room_veto": None,
         "heard_by": ["showay"], "last_seen": float(i)}
        for i in range(5)
    ]
    result = qualifying_theme_tags(rows, members=["showay"], now=100.0,
                                    used_tags_14d=set(), used_tag_today=False)
    assert [tag for tag, _ in result] == ["t4", "t3", "t2"]


# ── theme picks 排在最前面、去重、截長（驗收 9）─────────────────────────────

def test_merge_theme_picks_into_local_prepends_and_dedupes():
    theme_entries = [("1. A - 主題歌", {"id": "theme1"})]
    local_cands = [("1. A - 主題歌", {"id": "local_dup"}), ("2. B - 其他歌", {"id": "local2"})]
    merged = merge_theme_picks_into_local(theme_entries, local_cands, limit=40)
    assert merged[0] == ("1. A - 主題歌", {"id": "theme1"})
    assert len(merged) == 2


def test_merge_theme_picks_into_local_truncates_from_tail():
    theme_entries = [("theme", {})]
    local_cands = [(f"local{i}", {}) for i in range(5)]
    merged = merge_theme_picks_into_local(theme_entries, local_cands, limit=3)
    assert merged == [("theme", {}), ("local0", {}), ("local1", {})]
