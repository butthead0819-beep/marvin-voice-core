"""Tests for verifier_replay.py pure logic.

範圍：
  - build_verifier_user_prompt: 把 wake event + bids + context 拼成 user message
  - parse_verifier_response: 解析 70B 輸出 JSON，validate schema
  - classify_match: 比對 verifier 輸出 vs legacy outcome
  - aggregate_verifier_stats: 統計 verifier 是否補位 bus 失敗

不測 Groq HTTP 調用。
"""
from __future__ import annotations

import pytest

from scripts.verifier_replay import (
    VerifierResult,
    aggregate_verifier_stats,
    build_verifier_user_prompt,
    classify_match,
    parse_verifier_response,
)


# ── build_verifier_user_prompt ────────────────────────────────────────────────

def test_prompt_includes_raw_cleaned_speaker():
    out = build_verifier_user_prompt(
        raw="麻文播放周杰倫", cleaned="馬文，播放周杰倫", speaker="狗與露",
        bids=[("music", 0.55, "weak_play_long_string_no_marker"),
              ("nemoclaw", 0.0, "no_lobster_keyword")],
        recent_context=[],
    )
    assert "麻文播放周杰倫" in out
    assert "馬文，播放周杰倫" in out
    assert "狗與露" in out


def test_prompt_includes_bid_vector():
    out = build_verifier_user_prompt(
        raw="x", cleaned="x", speaker="a",
        bids=[("music", 0.55, "weak"), ("nemoclaw", 0.30, "borderline")],
        recent_context=[],
    )
    assert "music" in out
    assert "0.55" in out
    assert "nemoclaw" in out


def test_prompt_handles_empty_bids():
    out = build_verifier_user_prompt(
        raw="x", cleaned="x", speaker="a", bids=[], recent_context=[],
    )
    # 沒人 bid 也要可讀
    assert "no_bids" in out.lower() or "(空)" in out


def test_prompt_includes_recent_context():
    out = build_verifier_user_prompt(
        raw="x", cleaned="x", speaker="a", bids=[],
        recent_context=[("狗與露", "剛才那首歌叫什麼"), ("Marvin", "周杰倫的稻香")],
    )
    assert "剛才那首歌叫什麼" in out


# ── parse_verifier_response ───────────────────────────────────────────────────

def test_parse_valid_response():
    out = parse_verifier_response(
        '{"intent": "music", "confidence": 0.85, "reason": "music agent borderline + 用戶顯然要播歌"}'
    )
    assert out is not None
    assert out.intent == "music"
    assert out.confidence == 0.85
    assert "music" in out.reason


def test_parse_strips_code_fences():
    out = parse_verifier_response(
        '```json\n{"intent": "chat", "confidence": 0.7, "reason": ""}\n```'
    )
    assert out is not None and out.intent == "chat"


def test_parse_rejects_unknown_intent():
    out = parse_verifier_response(
        '{"intent": "unknown_label", "confidence": 0.5, "reason": "x"}'
    )
    assert out is None


def test_parse_rejects_malformed_json():
    assert parse_verifier_response("not json") is None
    assert parse_verifier_response("") is None


def test_parse_clamps_confidence():
    out = parse_verifier_response('{"intent": "drop", "confidence": 1.7, "reason": "x"}')
    assert out is not None and out.confidence == 1.0


# ── classify_match ────────────────────────────────────────────────────────────

def test_classify_match_music_with_music_play():
    # verifier 說 music，legacy 真的播了音樂 → match
    assert classify_match(verifier_intent="music", legacy_kind="music_play") == "match"


def test_classify_match_music_with_skip_command():
    # legacy 是 music skip 也算 music intent match
    assert classify_match(verifier_intent="music", legacy_kind="music_skip") == "match"


def test_classify_match_nemoclaw():
    assert classify_match(verifier_intent="nemoclaw", legacy_kind="nemoclaw") == "match"


def test_classify_match_chat_with_marvin_or_skip():
    # verifier 說 chat，legacy 走預設（馬文 LLM 或不接）→ match
    assert classify_match(verifier_intent="chat", legacy_kind="marvin_or_skip") == "match"


def test_classify_match_drop_with_marvin_or_skip():
    # drop 也算 match：legacy 不知道是 chat 還 drop，反正都沒走 music/nemo
    assert classify_match(verifier_intent="drop", legacy_kind="marvin_or_skip") == "match"


def test_classify_match_mismatch_music_vs_marvin():
    # verifier 說 music 但 legacy 沒播 → false positive (verifier over-trigger)
    assert classify_match(verifier_intent="music", legacy_kind="marvin_or_skip") == "fp_music"


def test_classify_match_mismatch_chat_vs_music():
    # verifier 說 chat 但 legacy 播了 music → false negative
    assert classify_match(verifier_intent="chat", legacy_kind="music_play") == "fn_music"


# ── aggregate_verifier_stats ──────────────────────────────────────────────────

def test_aggregate_counts_match_types():
    results = [
        VerifierResult(query="q1", legacy_kind="music_play", bus_winner="music",
                       verifier_intent="music", verifier_confidence=0.9,
                       verifier_reason="", verifier_latency_ms=1500,
                       bid_vector=[("music", 0.95, "")]),
        VerifierResult(query="q2", legacy_kind="music_play", bus_winner="no_bid",
                       verifier_intent="music", verifier_confidence=0.8,
                       verifier_reason="", verifier_latency_ms=1200,
                       bid_vector=[]),
        VerifierResult(query="q3", legacy_kind="marvin_or_skip", bus_winner="music",
                       verifier_intent="chat", verifier_confidence=0.7,
                       verifier_reason="", verifier_latency_ms=1100,
                       bid_vector=[("music", 0.55, "")]),
    ]
    stats = aggregate_verifier_stats(results)
    assert stats["n"] == 3
    # q1: verifier match, bus match → 共識
    # q2: verifier match, bus miss → verifier 補位
    # q3: verifier match, bus over-trigger → verifier 救回
    assert stats["verifier_matches"] == 3
    assert stats["bus_matches"] == 1
    assert stats["verifier_rescued_bus_failures"] == 2


def test_aggregate_zero_when_verifier_worse():
    results = [
        VerifierResult(query="q1", legacy_kind="music_play", bus_winner="music",
                       verifier_intent="chat", verifier_confidence=0.7,
                       verifier_reason="", verifier_latency_ms=1000,
                       bid_vector=[("music", 0.95, "")]),
    ]
    stats = aggregate_verifier_stats(results)
    # bus 對 verifier 錯 → 不算 rescue，反而算 verifier_introduced_failures
    assert stats["verifier_matches"] == 0
    assert stats["bus_matches"] == 1
    assert stats["verifier_introduced_failures"] == 1
