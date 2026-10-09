"""collision_reaction_report：上播組 vs holdout 組的口白前後 60 秒發言數差。"""
from __future__ import annotations

from scripts.collision_reaction_report import summarize


def _narr(nid, *, has_lyrics=True, kind=None, aired=False, holdout=False, ts=100.0):
    return {"ts": ts, "narration_id": nid, "has_lyrics": has_lyrics, "collision_kind": kind,
            "collision_aired": aired, "collision_holdout": holdout}


def _aired(nid, kind="full"):
    return {"type": "aired", "narration_id": nid, "kind": kind}


def _react(nid, pre, post):
    return {"type": "reaction", "narration_id": nid, "pre_60": pre, "post_60": post}


def test_hit_rate_counts_literal_among_narrations_with_lyrics():
    rows = [
        _narr("a", kind="literal", aired=True),
        _narr("b", kind="pinyin_only"),
        _narr("c"),
        _narr("d"),
        _narr("e", has_lyrics=False),
    ]
    s = summarize(rows)
    assert s["with_lyrics"] == 4
    assert s["literal"] == 1
    assert s["pinyin_only"] == 1


def test_groups_use_delta_and_only_full_aired_with_reaction():
    rows = [
        _narr("a1", kind="literal", aired=True), _aired("a1"), _react("a1", 1, 4),
        _narr("a2", kind="literal", aired=True), _aired("a2"), _react("a2", 2, 2),
        _narr("a3", kind="literal", aired=True), _aired("a3", "short"), _react("a3", 0, 9),
        _narr("a4", kind="literal", aired=True), _aired("a4"),
        _narr("h1", kind="literal", holdout=True), _aired("h1"), _react("h1", 3, 2),
        _narr("x", ), _aired("x"), _react("x", 0, 5),
    ]
    s = summarize(rows)
    assert s["aired"] == {"n": 2, "mean_delta": 1.5}
    assert s["holdout"] == {"n": 1, "mean_delta": -1.0}


def test_empty_group_has_no_mean():
    s = summarize([])
    assert s["aired"] == {"n": 0, "mean_delta": None}
    assert s["holdout"] == {"n": 0, "mean_delta": None}


def test_since_filters_narrations_by_ts():
    rows = [_narr("old", kind="literal", aired=True, ts=10.0), _narr("new", ts=200.0)]
    s = summarize(rows, since_ts=100.0)
    assert s["with_lyrics"] == 1
    assert s["literal"] == 0
