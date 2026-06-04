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


def test_pick_best_rejects_when_no_music_signal():
    # 2026-06-04 Gap B：全部候選都沒音樂信號（Music 類別/-Topic/MV/official…）→ 拒播 None。
    # 取代舊「總比沒結果好」fallback——STT 糊字搜出整排綜合影片時，寧可不播也不塞非音樂。
    candidates = [
        {"title": "Reaction A", "duration": 30, "categories": ["Entertainment"]},
        {"title": "Reaction B", "duration": 40, "categories": ["Entertainment"]},
    ]
    assert pick_best_music_candidate(candidates) is None


def test_pick_best_rejects_talk_show_garble_case():
    # 真實 case：「播放李欣」STT 糊字 → 搜出整排脫口秀精華，全無音樂信號 → 拒播。
    candidates = [
        {"title": "李新對公婆講話超直接 #小姐不熙娣【精華】", "uploader": "DeeGirlsTalk",
         "duration": 400, "categories": ["Entertainment"]},
        {"title": "李欣訪談完整版", "uploader": "新聞台", "duration": 500, "categories": []},
    ]
    assert pick_best_music_candidate(candidates) is None


def test_pick_best_keeps_topic_song_among_non_music():
    # 一排非音樂中夾一個「- Topic」音樂頻道 → 選那個音樂的。
    candidates = [
        {"title": "李欣訪談 reaction", "duration": 400, "categories": ["Entertainment"]},
        {"title": "愛人啊", "uploader": "孫淑媚 - Topic", "duration": 250, "categories": []},
    ]
    best = pick_best_music_candidate(candidates)
    assert best is not None
    assert best["uploader"] == "孫淑媚 - Topic"


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
