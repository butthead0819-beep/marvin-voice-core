"""Tests for DeclarativeIntentAgent base class.

範圍：
  - bid() 自動實作：gate → schema match → post-match filter → dense bid
  - gate 早退 → Bid(0.0, reason=gate_reason)
  - 無 schema 命中 → Bid(0.0, reason="no_match") (negative space)
  - schema 命中 + named groups → slots 傳入 reason_template
  - required_slots 空 → missing_slots
  - post_match_filter 拒絕 → 繼續找下個 schema
"""
from __future__ import annotations

import pytest

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_bus import IntentContext


def _ctx(query="", wake_intent=0.9, speaker="x", mode="normal"):
    return IntentContext(
        speaker=speaker, raw_text=query, query=query, original_raw=query,
        wake_intent=wake_intent, stream_active=False, game_mode=(mode == "game"),
        is_owner=False, now=0.0, mode=mode,
    )


class _ToyAgent(DeclarativeIntentAgent):
    """Toy agent for testing base class."""
    name = "toy"

    def __init__(self, intents=None, gate_reason=None, filter_reject=None):
        self._intents = intents or []
        self._gate_reason = gate_reason
        self._filter_reject = filter_reject  # set of schema names to reject

    def declare_intents(self):
        return self._intents

    def gate(self, ctx):
        return self._gate_reason

    def post_match_filter(self, schema, slots, ctx):
        if self._filter_reject and schema.name in self._filter_reject:
            return False
        return True


# ── gate behavior ─────────────────────────────────────────────────────────────

def test_gate_returns_dense_zero_bid():
    a = _ToyAgent(gate_reason="low_wake_intent")
    bid = a.bid(_ctx(query="anything"))
    assert bid is not None
    assert bid.confidence == 0.0
    assert bid.reason == "low_wake_intent"
    assert bid.name == "toy"


def test_empty_query_dense_zero():
    a = _ToyAgent(intents=[IntentSchema(name="play", confidence=0.9, patterns=["播放"])])
    bid = a.bid(_ctx(query=""))
    assert bid is not None
    assert bid.confidence == 0.0
    assert "empty" in bid.reason.lower()


# ── schema matching ───────────────────────────────────────────────────────────

def test_simple_pattern_match():
    schema = IntentSchema(name="play", confidence=0.95, patterns=["播放"],
                          reason_template="strong_play")
    a = _ToyAgent(intents=[schema])
    bid = a.bid(_ctx(query="幫我播放周杰倫"))
    assert bid.confidence == 0.95
    assert bid.reason == "strong_play"


def test_named_groups_extracted_to_slots():
    schema = IntentSchema(name="play", confidence=0.95,
                          patterns=[r"播放(?P<artist>\S+)"],
                          reason_template="play:{artist}")
    a = _ToyAgent(intents=[schema])
    bid = a.bid(_ctx(query="播放周杰倫"))
    assert "周杰倫" in bid.reason


def test_first_match_wins_order_preserved():
    s1 = IntentSchema(name="skip", confidence=0.95, patterns=["跳過"],
                      reason_template="skip")
    s2 = IntentSchema(name="pause", confidence=0.95, patterns=["暫停"],
                      reason_template="pause")
    a = _ToyAgent(intents=[s1, s2])
    # query 同時 match 兩個 → 第一個贏
    bid = a.bid(_ctx(query="跳過然後暫停"))
    assert bid.reason == "skip"


def test_no_match_returns_dense_zero():
    schema = IntentSchema(name="play", confidence=0.95, patterns=["播放"],
                          reason_template="play")
    a = _ToyAgent(intents=[schema])
    bid = a.bid(_ctx(query="今天天氣不錯"))
    assert bid.confidence == 0.0
    assert bid.reason == "no_match"


# ── required_slots → missing_slots ─────────────────────────────────────────────

def test_missing_required_slot_reported():
    schema = IntentSchema(name="play", confidence=0.55,
                          patterns=[r"播放(?P<artist>\S+)"],
                          required_slots=["song_title"],
                          reason_template="weak:{artist}")
    a = _ToyAgent(intents=[schema])
    bid = a.bid(_ctx(query="播放周杰倫"))
    assert bid.missing_slots == ["song_title"]


def test_present_slot_not_in_missing():
    schema = IntentSchema(name="play", confidence=0.95,
                          patterns=[r"播放(?P<artist>\S+)的(?P<song>\S+)"],
                          required_slots=["artist", "song"],
                          reason_template="strong:{artist}/{song}")
    a = _ToyAgent(intents=[schema])
    bid = a.bid(_ctx(query="播放周杰倫的稻香"))
    assert bid.missing_slots == []


# ── post_match_filter ──────────────────────────────────────────────────────────

def test_filter_rejection_continues_to_next_schema():
    s1 = IntentSchema(name="weak", confidence=0.55, patterns=["播放"],
                      reason_template="weak")
    s2 = IntentSchema(name="fallback", confidence=0.30, patterns=["播"],
                      reason_template="fallback")
    a = _ToyAgent(intents=[s1, s2], filter_reject={"weak"})
    bid = a.bid(_ctx(query="幫我播放陶喆"))
    # weak 被 reject，fallback 接住
    assert bid.reason == "fallback"
    assert bid.confidence == 0.30


def test_filter_rejection_all_falls_to_dense_zero():
    s1 = IntentSchema(name="weak", confidence=0.55, patterns=["播放"],
                      reason_template="weak")
    a = _ToyAgent(intents=[s1], filter_reject={"weak"})
    bid = a.bid(_ctx(query="播放陶喆"))
    assert bid.confidence == 0.0
    assert "no_match" in bid.reason or "filtered" in bid.reason.lower()


# ── handler wiring ────────────────────────────────────────────────────────────

def test_bid_always_has_handler():
    schema = IntentSchema(name="play", confidence=0.95, patterns=["播放"],
                          reason_template="play")
    a = _ToyAgent(intents=[schema])
    bid = a.bid(_ctx(query="播放"))
    assert bid.handler is not None
    # dense 0.0 也要有 handler（即使是 noop）
    bid2 = a.bid(_ctx(query="無關"))
    assert bid2.handler is not None


# ── mode_compatible ─────────────────────────────────────────────────────────────

def test_default_mode_compatible_is_normal_only():
    """Default: agent 只在 normal 模式出價。"""
    a = _ToyAgent(intents=[IntentSchema(name="play", confidence=0.9, patterns=["x"])])
    # default normal
    bid = a.bid(_ctx(query="x", mode="normal"))
    assert bid.confidence == 0.9
    # game 模式 → mode_mismatch dense 0.0
    bid_game = a.bid(_ctx(query="x", mode="game"))
    assert bid_game.confidence == 0.0
    assert bid_game.reason == "mode_mismatch:game"


class _StreamCapableAgent(_ToyAgent):
    mode_compatible = frozenset({"normal", "stream"})


def test_explicit_mode_compatible_allows_stream():
    a = _StreamCapableAgent(
        intents=[IntentSchema(name="play", confidence=0.9, patterns=["x"])]
    )
    assert a.bid(_ctx(query="x", mode="normal")).confidence == 0.9
    assert a.bid(_ctx(query="x", mode="stream")).confidence == 0.9
    # game 仍 mismatch
    assert a.bid(_ctx(query="x", mode="game")).reason == "mode_mismatch:game"


class _GameOnlyAgent(_ToyAgent):
    mode_compatible = frozenset({"game"})


def test_game_only_agent_silenced_in_normal():
    a = _GameOnlyAgent(
        intents=[IntentSchema(name="answer", confidence=1.0, patterns=["."])]
    )
    bid_normal = a.bid(_ctx(query="anything", mode="normal"))
    assert bid_normal.confidence == 0.0
    assert bid_normal.reason == "mode_mismatch:normal"
    # 只在 game 模式才接
    bid_game = a.bid(_ctx(query="anything", mode="game"))
    assert bid_game.confidence == 1.0


def test_mode_gate_takes_precedence_over_subclass_gate():
    """Subclass 的 gate 即使想出價，mode_mismatch 優先擋掉。"""
    class _AgentAlwaysBids(_ToyAgent):
        mode_compatible = frozenset({"normal"})
        # no override of gate() so super().gate() returns mode mismatch

    a = _AgentAlwaysBids(
        intents=[IntentSchema(name="x", confidence=0.9, patterns=["."])]
    )
    bid = a.bid(_ctx(query="x", mode="game"))
    assert bid.reason == "mode_mismatch:game"


# ── phonetic fallback（喚醒+指令音素 AND-gate → bid() 高信心捷徑）─────────────────
#
# regex 全 miss 後才試：wake 打中（wake_intent is None 或 >= PHONETIC_WAKE_MIN_INTENT）
# 且 schema.phonetic_keywords 拼音 fuzzy ratio >= PHONETIC_FUZZ_THRESHOLD 才出價。
# 兩個條件都要中（AND），任一沒中就是 no_match，不會生出行為。

def test_phonetic_fallback_hits_when_wake_and_command_both_match():
    schema = IntentSchema(
        name="skip_track", confidence=0.95, patterns=["下一首"],
        reason_template="skip", phonetic_keywords=["下一首"],
        phonetic_confidence=0.80,
    )
    a = _ToyAgent(intents=[schema])
    # STT 糊字：「下一手」regex 不中，但拼音跟「下一首」幾乎同音
    bid = a.bid(_ctx(query="下一手", wake_intent=0.9))
    assert bid.confidence == 0.80
    assert bid.reason.startswith("phonetic:")
    assert bid.handler is not None


def test_phonetic_fallback_requires_wake_hit_too():
    """指令拼音中但 wake 沒中（低於門檻）→ 不出價，AND-gate 擋下。"""
    schema = IntentSchema(
        name="skip_track", confidence=0.95, patterns=["下一首"],
        reason_template="skip", phonetic_keywords=["下一首"],
        phonetic_confidence=0.80,
    )
    a = _ToyAgent(intents=[schema])
    bid = a.bid(_ctx(query="下一手", wake_intent=0.3))
    assert bid.confidence == 0.0
    assert bid.reason == "no_match"


def test_phonetic_fallback_requires_command_hit_too():
    """wake 中但文字/拼音都對不上任何 phonetic_keywords → 不出價。"""
    schema = IntentSchema(
        name="skip_track", confidence=0.95, patterns=["下一首"],
        reason_template="skip", phonetic_keywords=["下一首"],
        phonetic_confidence=0.80,
    )
    a = _ToyAgent(intents=[schema])
    bid = a.bid(_ctx(query="今天天氣真好", wake_intent=0.9))
    assert bid.confidence == 0.0
    assert bid.reason == "no_match"


def test_phonetic_fallback_disabled_when_schema_opts_out():
    """schema 沒填 phonetic_keywords/phonetic_confidence → 完全不受影響（opt-in）。"""
    schema = IntentSchema(
        name="skip_track", confidence=0.95, patterns=["下一首"],
        reason_template="skip",
    )
    a = _ToyAgent(intents=[schema])
    bid = a.bid(_ctx(query="下一手", wake_intent=0.9))
    assert bid.confidence == 0.0
    assert bid.reason == "no_match"


def test_phonetic_fallback_does_not_shadow_exact_regex_match():
    """regex 先命中就直接用 regex 的 confidence，不會被 phonetic_confidence 蓋過。"""
    schema = IntentSchema(
        name="skip_track", confidence=0.95, patterns=["下一首"],
        reason_template="skip", phonetic_keywords=["下一首"],
        phonetic_confidence=0.80,
    )
    a = _ToyAgent(intents=[schema])
    bid = a.bid(_ctx(query="下一首", wake_intent=0.9))
    assert bid.confidence == 0.95
    assert bid.reason == "skip"


def test_phonetic_fallback_respects_post_match_filter():
    """post_match_filter 拒絕 → phonetic 命中也不算數（FP 防護不因音素路徑失效）。"""
    schema = IntentSchema(
        name="skip_track", confidence=0.95, patterns=["下一首"],
        reason_template="skip", phonetic_keywords=["下一首"],
        phonetic_confidence=0.80,
    )
    a = _ToyAgent(intents=[schema], filter_reject={"skip_track"})
    bid = a.bid(_ctx(query="下一手", wake_intent=0.9))
    assert bid.confidence == 0.0
    assert bid.reason == "no_match"


def test_phonetic_fallback_missing_required_slots_reported():
    schema = IntentSchema(
        name="query_time", confidence=0.9, patterns=["現在幾點"],
        required_slots=["topic"], reason_template="time",
        phonetic_keywords=["現在幾點"], phonetic_confidence=0.70,
    )
    a = _ToyAgent(intents=[schema])
    bid = a.bid(_ctx(query="現在及點", wake_intent=0.9))
    assert bid.confidence == 0.70
    assert bid.missing_slots == ["topic"]


def test_phonetic_fallback_matches_keyword_embedded_in_longer_sentence():
    """播放動詞混在整句裡（動詞後面還有歌名）也要中——不能只認整句=關鍵詞。"""
    schema = IntentSchema(
        name="phonetic_play", confidence=0.55, patterns=[],
        required_slots=["song_title"], reason_template="phonetic_play",
        phonetic_keywords=["播放"], phonetic_confidence=0.55,
    )
    a = _ToyAgent(intents=[schema])
    # 2 音節動詞窗口天生比長詞分數低（見 base.py docstring 實測），跟 MusicAgentV2
    # 一樣需要往下調門檻——這裡顯式設定，不依賴 85.0 預設值。
    a.PHONETIC_FUZZ_THRESHOLD = 75.0
    # STT 糊字：「播放」→「泡放」，後面還接了歌名
    bid = a.bid(_ctx(query="泡放周杰倫的稻香", wake_intent=0.9))
    assert bid.confidence == 0.55
    assert bid.missing_slots == ["song_title"]
