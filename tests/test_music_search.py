"""
TDD tests for music_search.py

驗證 YouTube 搜尋候選的評分與過濾邏輯：
- Music 類別加分
- 反應/實況/開箱/podcast 等黑名單關鍵字扣分
- 官方 MV / cover / 翻唱 等音樂提示加分
- 適當的歌曲長度（90s-10min）加分
- " - Topic" 結尾的 YouTube Music auto channel 加分
"""
from __future__ import annotations

import pytest

from music_search import (
    score_yt_candidate,
    pick_best_music_candidate,
    NON_MUSIC_BLACKLIST,
)


# ── 類別評分 ──────────────────────────────────────────────────────────────────

def test_music_category_boosts_score():
    info = {"title": "song", "duration": 200, "categories": ["Music"]}
    assert score_yt_candidate(info) >= 10


def test_gaming_category_penalized():
    info = {"title": "song", "duration": 200, "categories": ["Gaming"]}
    assert score_yt_candidate(info) < 0


def test_no_category_neutral():
    info = {"title": "song", "duration": 200, "categories": []}
    assert score_yt_candidate(info) >= 0


# ── 黑名單關鍵字 ──────────────────────────────────────────────────────────────

def test_reaction_video_penalized():
    info = {"title": "Song Reaction!", "duration": 300, "categories": []}
    assert score_yt_candidate(info) < 0


def test_chinese_reaction_penalized():
    info = {"title": "周杰倫 稻香 反應影片", "duration": 300, "categories": []}
    assert score_yt_candidate(info) < 0


def test_gameplay_penalized():
    info = {"title": "BGM gameplay walkthrough", "duration": 600, "categories": []}
    assert score_yt_candidate(info) < 0


def test_live_stream_penalized():
    info = {"title": "演唱會直播 LIVE Stream", "duration": 3600, "categories": []}
    assert score_yt_candidate(info) < 0


def test_unboxing_penalized():
    info = {"title": "黑膠唱片開箱 unboxing", "duration": 300, "categories": []}
    assert score_yt_candidate(info) < 0


# ── 音樂提示加分 ──────────────────────────────────────────────────────────────

def test_official_mv_boosted():
    info = {"title": "周杰倫 - 稻香 Official MV", "duration": 240, "categories": ["Music"]}
    assert score_yt_candidate(info) > 15


def test_cover_keyword_boosted():
    info = {"title": "稻香 Cover 翻唱", "duration": 240, "categories": []}
    # 有 cover/翻唱 → 加分（即使無類別）
    assert score_yt_candidate(info) > 0


def test_topic_channel_boosted():
    info = {
        "title": "稻香",
        "uploader": "周杰倫 - Topic",
        "duration": 240,
        "categories": [],
    }
    assert score_yt_candidate(info) > 0


# ── Duration 評分 ─────────────────────────────────────────────────────────────

def test_typical_song_length_boosted():
    info = {"title": "song", "duration": 240, "categories": []}
    assert score_yt_candidate(info) >= 3


def test_too_short_penalized():
    info = {"title": "song", "duration": 30, "categories": []}
    assert score_yt_candidate(info) < 0


def test_too_long_penalized():
    info = {"title": "song", "duration": 3600, "categories": []}
    assert score_yt_candidate(info) < 0


# ── pick_best_music_candidate ─────────────────────────────────────────────────

def test_pick_best_returns_highest_scoring():
    candidates = [
        {"title": "Song Reaction Video", "duration": 600, "categories": []},
        {"title": "周杰倫 稻香 Official MV", "duration": 240, "categories": ["Music"]},
        {"title": "稻香 gameplay BGM", "duration": 1800, "categories": ["Gaming"]},
    ]
    best = pick_best_music_candidate(candidates)
    assert "Official MV" in best["title"]


def test_pick_best_empty_returns_none():
    assert pick_best_music_candidate([]) is None


def test_pick_best_all_negative_still_returns_top():
    # 即使全部分數 < 0 也要回傳最高的（fallback，不要 None）
    candidates = [
        {"title": "Reaction A", "duration": 30, "categories": ["Entertainment"]},
        {"title": "Reaction B", "duration": 40, "categories": ["Entertainment"]},
    ]
    result = pick_best_music_candidate(candidates)
    assert result is not None


def test_pick_best_prefers_music_over_short_song():
    # 即使短歌有 "Music" 類別，正常長度的 Music 應該贏
    candidates = [
        {"title": "snippet", "duration": 25, "categories": ["Music"]},  # 太短扣分
        {"title": "full song", "duration": 200, "categories": ["Music"]},  # 正常
    ]
    best = pick_best_music_candidate(candidates)
    assert best["title"] == "full song"


# ── 黑名單常數驗證 ────────────────────────────────────────────────────────────

def test_blacklist_contains_common_non_music():
    keywords = [kw.lower() for kw in NON_MUSIC_BLACKLIST]
    for must_have in ("reaction", "gameplay", "podcast", "vlog", "實況", "開箱"):
        assert must_have in keywords, f"黑名單缺 {must_have}"
