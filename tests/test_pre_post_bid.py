"""Tests for pre_post_bid_harness.py pure logic.

範圍：
  - combine_bids: 取 raw_bid 與 cleaned_bid 的 best-of-both winner
  - classify_outcome: 比對 winner.name vs legacy_kind
  - aggregate_pre_post_stats: 統計三策略對 ground truth 的 agreement
"""
from __future__ import annotations

import pytest

from intent_bus import Bid
from scripts.pre_post_bid_harness import (
    PrePostRow,
    aggregate_pre_post_stats,
    classify_outcome,
    combine_bids,
)


def _bid(name, conf, reason="x"):
    async def _h():
        pass
    return Bid(name=name, confidence=conf, handler=_h, reason=reason)


# ── combine_bids ──────────────────────────────────────────────────────────────

def test_combine_takes_higher_confidence():
    raw = _bid("music", 0.95)
    cleaned = _bid("music", 0.55)
    winner = combine_bids(raw=raw, cleaned=cleaned)
    assert winner is not None
    assert winner.confidence == 0.95


def test_combine_handles_none_raw():
    cleaned = _bid("music", 0.80)
    assert combine_bids(raw=None, cleaned=cleaned) is cleaned


def test_combine_handles_none_cleaned():
    raw = _bid("music", 0.55)
    assert combine_bids(raw=raw, cleaned=None) is raw


def test_combine_handles_both_none():
    assert combine_bids(raw=None, cleaned=None) is None


def test_combine_different_agents_picks_higher():
    raw = _bid("nemoclaw", 0.95)
    cleaned = _bid("music", 0.55)
    winner = combine_bids(raw=raw, cleaned=cleaned)
    assert winner.name == "nemoclaw"


# ── classify_outcome ──────────────────────────────────────────────────────────

def test_classify_music_match():
    assert classify_outcome(winner_name="music", legacy_kind="music_play") == "match"


def test_classify_no_bid_match_default():
    assert classify_outcome(winner_name="no_bid", legacy_kind="marvin_or_skip") == "match"


def test_classify_music_fp_when_legacy_default():
    assert classify_outcome(winner_name="music", legacy_kind="marvin_or_skip") == "fp"


def test_classify_no_bid_fn_when_legacy_music():
    assert classify_outcome(winner_name="no_bid", legacy_kind="music_play") == "fn"


def test_classify_wrong_agent():
    # winner=music but legacy=nemoclaw → mismatch
    assert classify_outcome(winner_name="music", legacy_kind="nemoclaw") == "wrong_agent"


# ── aggregate_pre_post_stats ──────────────────────────────────────────────────

def test_aggregate_basic():
    rows = [
        PrePostRow(query="q1", legacy="music_play",
                   raw_winner="music", cleaned_winner="music", combined_winner="music"),
        PrePostRow(query="q2", legacy="marvin_or_skip",
                   raw_winner="no_bid", cleaned_winner="music", combined_winner="music"),
        PrePostRow(query="q3", legacy="music_play",
                   raw_winner="music", cleaned_winner="no_bid", combined_winner="music"),
    ]
    out = aggregate_pre_post_stats(rows)
    assert out["n"] == 3
    # cleaned_only (current bus): q1 match, q2 fp, q3 fn → 1/3
    assert out["cleaned_only_match_rate"] == pytest.approx(1 / 3, abs=0.01)
    # raw_only: q1 match, q2 match (no_bid=default), q3 match → 3/3
    assert out["raw_only_match_rate"] == pytest.approx(3 / 3, abs=0.01)
    # combined: q1 match, q2 fp, q3 match → 2/3
    assert out["combined_match_rate"] == pytest.approx(2 / 3, abs=0.01)


def test_aggregate_empty():
    assert aggregate_pre_post_stats([])["n"] == 0
