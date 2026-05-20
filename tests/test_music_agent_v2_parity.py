"""Behavior parity: MusicAgent v1 vs MusicAgentV2 on hand-picked cases.

每個 case 都驗 (confidence_bucket, missing_slots)；reason 字串可以不同
但格式要 parsable（reason starts with known prefix）。

Full 317-event replay 在 scripts/validate_music_v2_parity.py。
"""
from __future__ import annotations

import pytest

from intent_agents.music_agent import MusicAgent
from intent_agents.music_agent_v2 import MusicAgentV2
from intent_bus import IntentContext


class _FakeCtrl:
    """Stub controller with just the keyword constants."""
    _STRONG_PLAY_KW = ["放音樂", "播音樂", "放首歌", "播首歌", "放一首", "播一首",
                       "來首", "搜尋歌曲", "play music", "play song", "play some"]
    _WEAK_PLAY_KW = ["播放", "我想聽", "放點", "播點", "幫我找", "幫我放"]
    _MUSIC_SKIP_KW = ["換一首", "下一首", "跳過", "換歌", "不要這首", "skip"]
    _MUSIC_STOP_KW = ["停止播放", "音樂停", "不要播了", "關掉音樂", "停音樂",
                      "音樂關掉", "stop music", "stop playing"]
    _MUSIC_PAUSE_KW = ["暫停音樂", "暫停一下", "pause"]
    _MUSIC_RESUME_KW = ["繼續播", "繼續音樂", "播回來", "resume"]

    async def _safe_music_command(self, *a, **kw): pass
    async def _ask_music_followup(self, *a, **kw): pass


def _ctx(query, wake_intent=0.9):
    return IntentContext(
        speaker="x", raw_text=query, query=query, original_raw=query,
        wake_intent=wake_intent, stream_active=False, game_mode=False,
        is_owner=False, now=0.0,
    )


@pytest.fixture
def v1():
    return MusicAgent(_FakeCtrl())


@pytest.fixture
def v2():
    return MusicAgentV2(_FakeCtrl())


# ── Strong play (0.95) ───────────────────────────────────────────────────────

@pytest.mark.parametrize("query", [
    "馬文放音樂",
    "馬文，播首歌",
    "play some music",
])
def test_strong_play_both_bid_095(v1, v2, query):
    b1 = v1.bid(_ctx(query))
    b2 = v2.bid(_ctx(query))
    assert b1 is not None and b1.confidence == 0.95
    assert b2.confidence == 0.95
    assert b1.missing_slots == b2.missing_slots == []


# ── Weak play + marker (0.80) ────────────────────────────────────────────────

@pytest.mark.parametrize("query", [
    "馬文我想聽五月天的歌",   # 「的歌」後段<2字 → 仍 with_marker（非 specific）
])
def test_weak_play_with_marker_both_bid_080(v1, v2, query):
    b1 = v1.bid(_ctx(query))
    b2 = v2.bid(_ctx(query))
    assert b1.confidence == 0.80
    assert b2.confidence == 0.80


# ── v2 三檔分流刻意 diverge v1（5/21 vector intent，Gate 1 intentional）──────────
# 「artist的song（≥2字）」：v1 當 with_marker 0.80；v2 升 SPECIFIC 0.95（完整曲目）。
@pytest.mark.parametrize("query", [
    "馬文播放周杰倫的稻香",
])
def test_specific_v1_080_marker_v2_095(v1, v2, query):
    b1 = v1.bid(_ctx(query))
    b2 = v2.bid(_ctx(query))
    assert b1.confidence == 0.80          # v1 legacy 未變
    assert b2.confidence == 0.95          # v2 SPECIFIC
    assert b2.missing_slots == []


# 「artist-only（無歌名）」：v1 當 long_string 0.55 追問歌名；v2 升 CURATION 0.85，
# 缺 song_choice → 交給 semantic resolver 選歌（把選擇權交給 Marvin）。
@pytest.mark.parametrize("query", [
    "馬文播放周杰倫",
    "馬文幫我找陶喆",
])
def test_artist_only_v1_055_longstring_v2_085_curation(v1, v2, query):
    b1 = v1.bid(_ctx(query))
    b2 = v2.bid(_ctx(query))
    assert b1.confidence == 0.55          # v1 legacy 未變
    assert b1.missing_slots == ["song_title"]
    assert b2.confidence == 0.85          # v2 CURATION
    assert b2.missing_slots == ["song_choice"]


# ── Control intents (0.95) ───────────────────────────────────────────────────

@pytest.mark.parametrize("query,expected_prefix", [
    ("馬文，跳過", "control:skip"),
    ("馬文，暫停一下", "control:pause"),
    ("馬文，繼續播", "control:resume"),
    ("馬文，停止播放", "control:stop"),
])
def test_control_intents_match(v1, v2, query, expected_prefix):
    b1 = v1.bid(_ctx(query))
    b2 = v2.bid(_ctx(query))
    assert b1.confidence == 0.95
    assert b2.confidence == 0.95
    assert b2.reason.startswith(expected_prefix), f"v2 reason: {b2.reason}"


# ── Gates: low wake_intent → v1 None, v2 dense 0.0 ───────────────────────────

def test_low_wake_intent_v1_none_v2_dense_zero(v1, v2):
    b1 = v1.bid(_ctx("馬文播放音樂", wake_intent=0.3))
    b2 = v2.bid(_ctx("馬文播放音樂", wake_intent=0.3))
    assert b1 is None
    assert b2 is not None
    assert b2.confidence == 0.0
    assert b2.reason == "low_wake_intent"


def test_no_keyword_v1_none_v2_dense_zero(v1, v2):
    b1 = v1.bid(_ctx("馬文你好嗎"))
    b2 = v2.bid(_ctx("馬文你好嗎"))
    assert b1 is None
    assert b2 is not None
    assert b2.confidence == 0.0
    assert b2.reason == "no_match"


def test_repetitive_hallucination_both_skip(v1, v2):
    query = "播放陶喆 播放陶喆 播放陶喆 播放陶喆"
    b1 = v1.bid(_ctx(query))
    b2 = v2.bid(_ctx(query))
    assert b1 is None
    assert b2.confidence == 0.0
    assert b2.reason == "repetitive_hallucination"


# ── UI blocklist (NON_MUSIC_TARGETS) ─────────────────────────────────────────

@pytest.mark.parametrize("query", [
    "馬文播放這個",
    "馬文播放那個",
    "馬文播放控制",
])
def test_ui_blocklist_both_no_match(v1, v2, query):
    b1 = v1.bid(_ctx(query))
    b2 = v2.bid(_ctx(query))
    assert b1 is None
    # v2 不是 None，但是 dense 0.0 + "no_match"（schema 命中但 filter reject）
    assert b2.confidence == 0.0
