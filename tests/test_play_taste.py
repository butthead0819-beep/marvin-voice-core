"""play_taste.py 純函式測試。"""
from __future__ import annotations

from play_taste import REINFORCE_GAP_S, REINFORCE_REASON, artist_candidates, keys_to_reinforce

NOW = 1_700_000_000.0


def test_artist_candidates_collects_title_artist_uploader_dedup_and_drops_unknown():
    info = {
        "title": "盧廣仲 Crowd Lu【淵明】Official Music Video",
        "artist": "未知藝術家",
        "uploader": "添翼音樂 TEAM EAR MUSIC",
    }
    assert artist_candidates(info) == ["盧廣仲", "添翼音樂 TEAM EAR MUSIC"]


def test_artist_candidates_dedup_when_title_and_artist_match():
    info = {"title": "周杰倫 Jay Chou【晴天】", "artist": "周杰倫", "uploader": "周杰倫"}
    assert artist_candidates(info) == ["周杰倫"]


def test_artist_candidates_hits_via_uploader():
    info = {"title": "日不落", "uploader": "JOLIN 蔡依林"}
    assert artist_candidates(info) == ["JOLIN 蔡依林"]


def test_keys_to_reinforce_basic_hit():
    taste = {"盧廣仲": {"score": 1.5, "last_update": 0.0}}
    assert keys_to_reinforce(taste, set(), ["盧廣仲"], NOW) == ["盧廣仲"]


def test_keys_to_reinforce_substring_match():
    taste = {"張雨生音樂": {"score": 2.0, "last_update": 0.0}}
    assert keys_to_reinforce(taste, set(), ["張雨生"], NOW) == ["張雨生音樂"]


def test_keys_to_reinforce_non_positive_score_excluded():
    taste = {"盧廣仲": {"score": 0.0, "last_update": 0.0}}
    assert keys_to_reinforce(taste, set(), ["盧廣仲"], NOW) == []


def test_keys_to_reinforce_taboo_excluded():
    taste = {"盧廣仲": {"score": 1.5, "last_update": 0.0}}
    assert keys_to_reinforce(taste, {"盧廣仲"}, ["盧廣仲"], NOW) == []


def test_keys_to_reinforce_unrelated_key_excluded():
    taste = {"清酒": {"score": 1.5, "last_update": 0.0}}
    assert keys_to_reinforce(taste, set(), ["盧廣仲"], NOW) == []


def test_keys_to_reinforce_recent_song_request_gap_excluded():
    taste = {"盧廣仲": {
        "score": 1.5,
        "last_update": NOW - 23 * 3600,
        "last_reason": REINFORCE_REASON,
    }}
    assert keys_to_reinforce(taste, set(), ["盧廣仲"], NOW) == []


def test_keys_to_reinforce_past_gap_included():
    taste = {"盧廣仲": {
        "score": 1.5,
        "last_update": NOW - 25 * 3600,
        "last_reason": REINFORCE_REASON,
    }}
    assert keys_to_reinforce(taste, set(), ["盧廣仲"], NOW) == ["盧廣仲"]


def test_keys_to_reinforce_recent_but_different_reason_included():
    taste = {"盧廣仲": {
        "score": 1.5,
        "last_update": NOW - 23 * 3600,
        "last_reason": "daily_review",
    }}
    assert keys_to_reinforce(taste, set(), ["盧廣仲"], NOW) == ["盧廣仲"]


def test_keys_to_reinforce_empty_candidates():
    taste = {"盧廣仲": {"score": 1.5, "last_update": 0.0}}
    assert keys_to_reinforce(taste, set(), [], NOW) == []


def test_reinforce_gap_is_24h():
    assert REINFORCE_GAP_S == 86400.0
