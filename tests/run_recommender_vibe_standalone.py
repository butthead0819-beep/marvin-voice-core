"""
tests/run_recommender_vibe_standalone.py — M3 vibe_filter + pick_candidates

Run: venv_simon/bin/python tests/run_recommender_vibe_standalone.py
"""
from __future__ import annotations

import random
import sys
import traceback
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import music_recommender as mr


PASSED = 0
FAILED = 0
FAILURES = []


def run(name, fn):
    global PASSED, FAILED
    try:
        fn()
        print(f"  ✓ {name}")
        PASSED += 1
    except AssertionError as e:
        print(f"  ✗ {name}: {e}")
        FAILURES.append((name, traceback.format_exc()))
        FAILED += 1
    except Exception as e:
        print(f"  ✗ {name} ERROR: {type(e).__name__}: {e}")
        FAILURES.append((name, traceback.format_exc()))
        FAILED += 1


# ── 共用 fixture-like helpers ────────────────────────────────────────────────

def _mk_song(title, uploader="", connections=None, requesters=None, feelings_by_user=None, last_play_ts=0.0):
    """製造 music_memory 風格的 song dict。"""
    song = {
        "title": title,
        "uploader": uploader,
        "connections": list(connections or []),
        "requesters": dict(requesters or {}),
        "reactions": {},
        "plays": [{"ts": last_play_ts}] if last_play_ts else [],
    }
    if feelings_by_user:
        for user, feelings in feelings_by_user.items():
            song["reactions"][user] = {"feelings": list(feelings)}
    return song


# ── vibe_filter None backward-compat ─────────────────────────────────────────

def t_vibe_filter_none_unchanged():
    """vibe_filter=None 行為應該完全跟舊版一樣。"""
    songs = {
        "s1": _mk_song("Song A", connections=["alice", "bob"], requesters={"alice": 3}),
        "s2": _mk_song("Song B", connections=["alice", "bob"], requesters={"bob": 5}),
    }
    pool_no_vibe = mr.build_recommendation_pool(
        members=["alice", "bob"], songs=songs, exclude_titles=[],
        now=1000.0, vibe_filter=None,
    )
    pool_no_param = mr.build_recommendation_pool(
        members=["alice", "bob"], songs=songs, exclude_titles=[],
        now=1000.0,
    )
    assert len(pool_no_vibe) == len(pool_no_param)
    for a, b in zip(pool_no_vibe, pool_no_param):
        assert a.score == b.score, f"score 不一致: {a.score} vs {b.score}"


# ── vibe_filter mood boost ───────────────────────────────────────────────────

def t_vibe_chill_boosts_chill_feeling_song():
    """mood=放鬆 應該 boost feelings 命中 chill keywords 的歌。"""
    songs = {
        "s1": _mk_song(
            "Chill Song", connections=["alice", "bob"],
            requesters={"alice": 1},
            feelings_by_user={"alice": ["chill", "夜晚", "舒服"]},
        ),
        "s2": _mk_song(
            "Normal Song", connections=["alice", "bob"],
            requesters={"alice": 1},
            feelings_by_user={"alice": []},
        ),
    }
    pool = mr.build_recommendation_pool(
        members=["alice", "bob"], songs=songs, exclude_titles=[],
        now=1000.0, vibe_filter={"mood": "放鬆"},
    )
    # Chill Song 應該排在 Normal Song 之前（boosted）
    titles = [c.anchor_title for c in pool]
    assert titles[0] == "Chill Song", f"expect Chill first got {titles}"


def t_vibe_split_boosts_group_resonance_lane():
    """mood=分歧 應該 boost group_resonance lane（中介曲）。"""
    songs = {
        "resonant": _mk_song(
            "Bridge Song", connections=["alice", "bob"],
            requesters={"alice": 1, "bob": 1},
        ),
    }
    pool_no_vibe = mr.build_recommendation_pool(
        members=["alice", "bob"], songs=songs, exclude_titles=[],
        now=1000.0,
    )
    pool_split = mr.build_recommendation_pool(
        members=["alice", "bob"], songs=songs, exclude_titles=[],
        now=1000.0, vibe_filter={"mood": "分歧"},
    )
    assert pool_split[0].score > pool_no_vibe[0].score, \
        f"split mood 應該 boost group_resonance ({pool_split[0].score} vs {pool_no_vibe[0].score})"


def t_vibe_unknown_mood_no_boost():
    """mood 不在 4 檔內 → 不 crash、不 boost。"""
    songs = {
        "s1": _mk_song("X", connections=["alice", "bob"], requesters={"alice": 1}),
    }
    pool = mr.build_recommendation_pool(
        members=["alice", "bob"], songs=songs, exclude_titles=[],
        now=1000.0, vibe_filter={"mood": "未知"},
    )
    pool_none = mr.build_recommendation_pool(
        members=["alice", "bob"], songs=songs, exclude_titles=[],
        now=1000.0,
    )
    assert pool[0].score == pool_none[0].score


# ── min_score filter ─────────────────────────────────────────────────────────

def t_min_score_filters_low_candidates():
    songs = {
        "high": _mk_song("High", connections=["alice", "bob"], requesters={"alice": 1}),  # group_resonance score=120
        "low": _mk_song(
            "Low", requesters={"alice": 1},  # long_tail only if old
            last_play_ts=0.0,  # very old → long_tail
        ),
    }
    pool = mr.build_recommendation_pool(
        members=["alice", "bob"], songs=songs, exclude_titles=[],
        now=10 * 86400.0,
        vibe_filter={"min_score": 100.0},
    )
    titles = [c.anchor_title for c in pool]
    assert "High" in titles
    assert "Low" not in titles, f"Low (score~70) 應該被 min_score=100 過濾掉, got {titles}"


# ── pick_candidates ──────────────────────────────────────────────────────────

def t_pick_candidates_empty_pool():
    assert mr.pick_candidates([]) == []


def t_pick_candidates_fewer_than_k():
    cs = [mr.Candidate("A", "", "group_resonance", "direct", None, 100.0)]
    assert mr.pick_candidates(cs, k=3) == cs


def t_pick_candidates_returns_k_unique():
    cs = [
        mr.Candidate(f"S{i}", "", "group_resonance", "direct", None, 100.0 - i)
        for i in range(9)
    ]
    picked = mr.pick_candidates(cs, k=3, top_n=9, rng=random.Random(42))
    assert len(picked) == 3
    titles = [c.anchor_title for c in picked]
    assert len(set(titles)) == 3, f"應該 3 個不重複 got {titles}"


def t_pick_candidates_uses_top_n():
    """k=3 top_n=5 → 只從前 5 個抽，後 5 個（低分）不會被抽到。"""
    cs = [
        mr.Candidate(f"S{i}", "", "group_resonance", "direct", None, 100.0 - i)
        for i in range(10)
    ]
    rng = random.Random(123)
    # 跑 50 次、檢查 S5+ 從未出現
    for _ in range(50):
        picked = mr.pick_candidates(cs, k=3, top_n=5, rng=rng)
        for c in picked:
            idx = int(c.anchor_title[1:])
            assert idx < 5, f"top_n=5 但選到 S{idx}"


def t_pick_candidates_weighted_high_score_more_likely():
    """高分 candidate 應該比低分更常被選。"""
    cs = [
        mr.Candidate("High", "", "group_resonance", "direct", None, 1000.0),
        mr.Candidate("Low1", "", "group_resonance", "direct", None, 1.0),
        mr.Candidate("Low2", "", "group_resonance", "direct", None, 1.0),
        mr.Candidate("Low3", "", "group_resonance", "direct", None, 1.0),
    ]
    rng = random.Random(7)
    counter = Counter()
    for _ in range(200):
        picked = mr.pick_candidates(cs, k=1, top_n=4, rng=rng)
        counter[picked[0].anchor_title] += 1
    # High 應該被選的次數遠大於每個 Low
    assert counter["High"] > 100, f"High weighted 不夠 (counter={counter})"


# ── pick_candidate (legacy) 不破壞 ───────────────────────────────────────────

def t_legacy_pick_candidate_still_works():
    cs = [
        mr.Candidate("A", "", "group_resonance", "direct", None, 100.0),
        mr.Candidate("B", "", "long_tail", "direct", None, 50.0),
    ]
    rng = random.Random(0)
    c = mr.pick_candidate(cs, top_n=2, rng=rng)
    assert c is not None
    assert c.anchor_title in ("A", "B")


def t_legacy_pick_candidate_empty_returns_none():
    assert mr.pick_candidate([]) is None


# ── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== M3 music_recommender.py (vibe_filter + pick_candidates) ===\n")

    print("Backward compat:")
    run("vibe_filter=None unchanged", t_vibe_filter_none_unchanged)
    print()

    print("vibe_filter mood boost:")
    run("放鬆 boosts chill feeling song", t_vibe_chill_boosts_chill_feeling_song)
    run("分歧 boosts group_resonance lane", t_vibe_split_boosts_group_resonance_lane)
    run("unknown mood no boost no crash", t_vibe_unknown_mood_no_boost)
    print()

    print("min_score filter:")
    run("min_score filters low candidates", t_min_score_filters_low_candidates)
    print()

    print("pick_candidates:")
    run("empty pool → []", t_pick_candidates_empty_pool)
    run("fewer than k → return all", t_pick_candidates_fewer_than_k)
    run("returns k unique", t_pick_candidates_returns_k_unique)
    run("respects top_n", t_pick_candidates_uses_top_n)
    run("weighted: high score more likely", t_pick_candidates_weighted_high_score_more_likely)
    print()

    print("Legacy pick_candidate:")
    run("still works", t_legacy_pick_candidate_still_works)
    run("empty → None", t_legacy_pick_candidate_empty_returns_none)

    print()
    print(f"=== Results: {PASSED} passed, {FAILED} failed ===")
    if FAILED:
        print("\n--- Failures ---")
        for name, tb in FAILURES:
            print(f"\n{name}:")
            print(tb)
        sys.exit(1)
