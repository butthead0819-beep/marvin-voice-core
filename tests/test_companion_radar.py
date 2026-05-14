"""防呆雷達 (companion_radar) — 規則式風險分類器測試。

classify_risk(text, context) → dict | None

不呼叫 LLM；純 regex / keyword 規則。
回傳 None 表示安全；dict {rule, reason, severity} 表示風險。
"""
from __future__ import annotations

import pytest

from marvin_voice_core.companion_radar import classify_risk


# ── defeat_jab：玩家剛輸了，再被嘲笑 ─────────────────────────────────

def test_classify_risk_defeat_jab_matches():
    context = {
        "recent_game_events": [
            {"type": "lost_round", "user": "Bob"},
        ],
    }
    result = classify_risk("Bob 又輸了，真是廢物", context)
    assert result is not None
    assert result["rule"] == "defeat_jab"
    assert result["severity"] in ("medium", "high")
    assert isinstance(result.get("reason"), str)
    assert len(result["reason"]) > 0


def test_classify_risk_defeat_jab_other_lose_keywords():
    """『敗』『輸光』『沒贏』也算 defeat keyword。"""
    context = {"recent_game_events": [{"type": "lost_round", "user": "Bob"}]}
    assert classify_risk("Bob 整個敗了", context) is not None
    assert classify_risk("輸光啦", context) is not None


# ── tone_mismatch_serious_to_joke：嚴肅氣氛開玩笑 ───────────────────

def test_classify_risk_tone_mismatch_matches():
    context = {
        "atmosphere_snapshot": {"room_mood": "認真討論"},
    }
    result = classify_risk("哈哈哈 笑死", context)
    assert result is not None
    assert result["rule"] == "tone_mismatch_serious_to_joke"


def test_classify_risk_tone_mismatch_serious_mood_match():
    context = {
        "atmosphere_snapshot": {"room_mood": "嚴肅"},
    }
    result = classify_risk("lol", context)
    assert result is not None
    assert result["rule"] == "tone_mismatch_serious_to_joke"


# ── sarcasm_to_negative_bias_target：對 bias 低的人講風涼話 ───────

def test_classify_risk_sarcasm_matches():
    context = {
        "target_player": "Bob",
        "player_memory": {"bias_score": -5},
    }
    result = classify_risk("Bob 真聰明", context)
    assert result is not None
    assert result["rule"] == "sarcasm_to_negative_bias_target"
    assert result["severity"] == "high"


def test_classify_risk_sarcasm_with_棒_marker():
    context = {
        "target_player": "Bob",
        "player_memory": {"bias_score": -10},
    }
    result = classify_risk("Bob 真棒", context)
    assert result is not None
    assert result["rule"] == "sarcasm_to_negative_bias_target"


# ── 安全文本 / 缺 context ─────────────────────────────────────────

def test_classify_risk_safe_text_returns_none():
    """正常對話 + 無風險上下文 → None。"""
    context = {
        "atmosphere_snapshot": {"room_mood": "放鬆閒聊"},
        "recent_game_events": [],
    }
    assert classify_risk("今天天氣不錯", context) is None


def test_classify_risk_no_context_returns_none():
    """context 為空 dict → 無從判斷 → None。"""
    assert classify_risk("Bob 輸了", {}) is None
    assert classify_risk("哈哈哈", {}) is None


def test_classify_risk_bias_above_threshold_returns_none():
    """target 的 bias_score ≥ -3 → 不算 sarcasm。"""
    context = {
        "target_player": "Bob",
        "player_memory": {"bias_score": 0},
    }
    assert classify_risk("Bob 真聰明", context) is None


def test_classify_risk_mood_casual_with_jokes_returns_none():
    """放鬆氣氛開玩笑 → 不算 tone_mismatch。"""
    context = {"atmosphere_snapshot": {"room_mood": "放鬆閒聊"}}
    assert classify_risk("哈哈哈", context) is None


def test_classify_risk_defeat_keyword_without_lost_event_returns_none():
    """有 defeat keyword 但 recent_game_events 沒人輸 → 不算 defeat_jab。"""
    context = {"recent_game_events": [{"type": "joined", "user": "Bob"}]}
    assert classify_risk("Bob 輸了", context) is None
