"""TDD — 點歌佇列空後的自動推薦：候選池 builder（純函式，無 Discord/LLM）。

目標：把「變化」與「團體聚合」從便宜 LLM 移到確定性 Python。
  - 三條 lane：group_resonance / long_tail / spotlight
  - exclude 用正規化標題比對（去掉 (cover)/(Live)/feat. 等變體）
  - 在場成員聚合：≥2 在場者共鳴的歌優先
"""
from __future__ import annotations

import random

from music_recommender import (
    Candidate,
    build_recommendation_pool,
    normalize_title,
    pick_candidate,
)


NOW = 1_700_000_000.0
DAY = 86400.0


def _song(title, *, requesters=None, connections=None, last_play_age_days=0.0, uploader="orig"):
    ts = NOW - last_play_age_days * DAY
    return {
        "title": title,
        "uploader": uploader,
        "url": f"http://x/{title}",
        "total_plays": sum((requesters or {}).values()) or 1,
        "plays": [{"by": b, "ts": ts} for b in (requesters or {"a": 1})],
        "requesters": dict(requesters or {}),
        "connections": list(connections or []),
    }


# ── normalize_title ───────────────────────────────────────────────────────────

def test_normalize_title_strips_variant_suffixes():
    base = normalize_title("晴天")
    assert normalize_title("晴天 (cover)") == base
    assert normalize_title("晴天 (Live)") == base
    assert normalize_title("晴天 feat. someone") == base
    assert normalize_title("  晴天  ") == base


def test_normalize_title_case_insensitive():
    assert normalize_title("Yesterday") == normalize_title("yesterday")


# ── group_resonance lane ───────────────────────────────────────────────────────

def test_group_resonance_ranks_shared_song_first():
    songs = {
        "s1": _song("孤芳自賞", requesters={"Alice": 1}),
        "s2": _song("大家的歌", requesters={"Alice": 1, "Bob": 1}, connections=["Alice", "Bob"]),
    }
    pool = build_recommendation_pool(
        members=["Alice", "Bob"], songs=songs, exclude_titles=[], now=NOW,
    )
    assert pool, "應有候選"
    top = pool[0]
    assert top.lane == "group_resonance"
    assert normalize_title(top.anchor_title) == normalize_title("大家的歌")
    assert top.mode == "direct"


def test_group_resonance_requires_two_present_members():
    # connections 只含一位在場者 → 不算群體共鳴
    songs = {"s": _song("獨愛", requesters={"Alice": 1}, connections=["Alice"])}
    pool = build_recommendation_pool(
        members=["Alice", "Bob"], songs=songs, exclude_titles=[], now=NOW,
    )
    assert all(c.lane != "group_resonance" for c in pool)


# ── spotlight lane ─────────────────────────────────────────────────────────────

def test_spotlight_picks_target_member_top_song_as_cover():
    songs = {
        "s1": _song("阿明最愛", requesters={"阿明": 9}),
        "s2": _song("阿明普通", requesters={"阿明": 2}),
        "s3": _song("別人的歌", requesters={"Other": 5}),
    }
    pool = build_recommendation_pool(
        members=["阿明", "Other"], songs=songs, exclude_titles=[], now=NOW,
        spotlight_member="阿明",
    )
    spot = [c for c in pool if c.lane == "spotlight"]
    assert spot, "應有 spotlight 候選"
    assert spot[0].target_member == "阿明"
    assert normalize_title(spot[0].anchor_title) == normalize_title("阿明最愛")
    assert spot[0].mode == "cover"


# ── long_tail lane ─────────────────────────────────────────────────────────────

def test_long_tail_includes_old_songs_as_direct():
    songs = {
        "old": _song("塵封老歌", requesters={"Alice": 1}, last_play_age_days=30),
        "new": _song("昨天剛播", requesters={"Alice": 1}, last_play_age_days=0.5),
    }
    pool = build_recommendation_pool(
        members=["Alice"], songs=songs, exclude_titles=[], now=NOW,
    )
    lt = [c for c in pool if c.lane == "long_tail"]
    titles = {normalize_title(c.anchor_title) for c in lt}
    assert normalize_title("塵封老歌") in titles
    assert normalize_title("昨天剛播") not in titles  # 太新，不算長尾
    assert all(c.mode == "direct" for c in lt)


# ── exclude（正規化比對）────────────────────────────────────────────────────────

def test_exclude_removes_by_normalized_title():
    songs = {"s": _song("大家的歌", requesters={"Alice": 1, "Bob": 1}, connections=["Alice", "Bob"])}
    pool = build_recommendation_pool(
        members=["Alice", "Bob"], songs=songs,
        exclude_titles=["大家的歌 (cover)"],  # 變體字串也要擋掉
        now=NOW,
    )
    assert pool == []


# ── 空池 ────────────────────────────────────────────────────────────────────────

def test_empty_when_no_member_history():
    songs = {"s": _song("陌生人的歌", requesters={"Stranger": 3})}
    pool = build_recommendation_pool(
        members=["Alice"], songs=songs, exclude_titles=[], now=NOW,
    )
    assert pool == []


# ── pick_candidate（加權隨機抽樣 = 變化來源）─────────────────────────────────────

def _cand(title, score):
    return Candidate(title, "artist", "long_tail", "direct", None, score)


def test_pick_candidate_none_for_empty_pool():
    assert pick_candidate([], rng=random.Random(0)) is None


def test_pick_candidate_returns_within_top_n():
    pool = [_cand(f"歌{i}", 100 - i) for i in range(10)]
    chosen = pick_candidate(pool, rng=random.Random(1), top_n=3)
    assert chosen.anchor_title in {"歌0", "歌1", "歌2"}


def test_pick_candidate_varies_across_seeds():
    """不同 seed 會抽到不同首 → 確認有變化（非永遠最高分）。"""
    pool = [_cand(f"歌{i}", 50) for i in range(5)]  # 同分 → 純隨機
    picks = {pick_candidate(pool, rng=random.Random(s), top_n=5).anchor_title for s in range(20)}
    assert len(picks) > 1
