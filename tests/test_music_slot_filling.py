"""TDD: P1 — MusicAgent 弱訊號 artist-only → missing_slots + follow-up 提問。

5/18 真實 case：
  '播放陶喆' → MusicAgent bid 0.55 weak_play_only → yt-dlp 搜「陶喆」
  → 抽中「Susan說」「浪流連」等錯歌，user 體感「點歌亂選」。

Alexa CanFulfillIntent 的 missing_slots 概念：bid 知道自己缺資料，
讓 controller 改問「陶喆的哪一首？」而不是賭一首歌。

P1 scope（刻意縮小）：
- Bid.missing_slots 欄位（觀測 + handler 路由依據）
- MusicAgent 0.55 case → missing_slots=["song_title"]，handler 改打
  _ask_music_followup 不打 _safe_music_command
- 0.80 (marker hit) / 0.95 (strong play / control) → missing_slots 空
- 不做 per-speaker pending state（user 需要重新喚醒講全名；比亂選歌好）
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_bus import IntentContext, Bid
from intent_agents.music_agent import MusicAgent


def _ctx(query, wake_intent=None):
    return IntentContext(
        speaker="Alice", raw_text=query, query=query, original_raw=query,
        wake_intent=wake_intent, stream_active=False,
        game_mode=False, is_owner=False, now=100.0,
    )


def _agent():
    from cogs.voice_controller import VoiceController as _VC
    ctrl = MagicMock()
    ctrl._STRONG_PLAY_KW   = _VC._STRONG_PLAY_KW
    ctrl._WEAK_PLAY_KW     = _VC._WEAK_PLAY_KW
    ctrl._MUSIC_SKIP_KW    = _VC._MUSIC_SKIP_KW
    ctrl._MUSIC_STOP_KW    = _VC._MUSIC_STOP_KW
    ctrl._MUSIC_PAUSE_KW   = _VC._MUSIC_PAUSE_KW
    ctrl._MUSIC_RESUME_KW  = _VC._MUSIC_RESUME_KW
    ctrl._safe_music_command = AsyncMock()
    ctrl._ask_music_followup = AsyncMock()
    return MusicAgent(ctrl), ctrl


# ── Bid 欄位契約 ──────────────────────────────────────────────────────────

def test_bid_missing_slots_defaults_to_empty():
    """Bid() 沒指定 missing_slots 時應該 default 空 list（不是 None，避免 caller None-check）。"""
    bid = Bid(name="x", confidence=0.5, handler=AsyncMock(), reason="t")
    assert bid.missing_slots == []


def test_bid_missing_slots_per_instance_independent():
    """default_factory list 不能共享（regression guard）。"""
    a = Bid(name="a", confidence=0.5, handler=AsyncMock(), reason="t")
    b = Bid(name="b", confidence=0.5, handler=AsyncMock(), reason="t")
    a.missing_slots.append("x")
    assert b.missing_slots == [], "missing_slots 不該跨 instance 共享 list"


# ── MusicAgent: 0.55 case missing slots ───────────────────────────────────

@pytest.mark.parametrize("query", [
    "播放周杰倫",
    "播放Adele",
    "播放陶喆",
])
def test_weak_play_artist_only_declares_missing_song_title(query):
    agent, _ = _agent()
    bid = agent.bid(_ctx(query))
    assert bid is not None
    assert bid.confidence == pytest.approx(0.55)
    assert bid.missing_slots == ["song_title"], \
        f"0.55 case '{query}' 應該宣告缺 song_title，實際 {bid.missing_slots}"


# ── MusicAgent: 0.80 / 0.95 不該標 missing ────────────────────────────────

@pytest.mark.parametrize("query", [
    "播放陶喆的天天",
    "我想聽周杰倫的稻香",
    "放點輕音樂",
    "幫我找一首抒情曲",
])
def test_weak_play_with_marker_no_missing(query):
    agent, _ = _agent()
    bid = agent.bid(_ctx(query))
    assert bid is not None
    assert bid.confidence == pytest.approx(0.80)
    assert bid.missing_slots == [], f"0.80 case '{query}' 不該宣告 missing"


@pytest.mark.parametrize("query", [
    "放音樂",
    "放首歌",
    "播首歌",
    "來首老歌",
    "play music",
])
def test_strong_play_no_missing(query):
    agent, _ = _agent()
    bid = agent.bid(_ctx(query))
    assert bid is not None
    assert bid.confidence == pytest.approx(0.95)
    assert bid.missing_slots == [], f"strong play '{query}' 不該宣告 missing"


@pytest.mark.parametrize("query", [
    "換一首",
    "跳過",
    "停止播放",
    "暫停音樂",
    "繼續播",
])
def test_control_cmd_no_missing(query):
    agent, _ = _agent()
    bid = agent.bid(_ctx(query))
    assert bid is not None
    assert bid.missing_slots == [], f"control '{query}' 不該宣告 missing"


# ── Handler 路由：missing → followup，無 missing → play ──────────────────

@pytest.mark.asyncio
async def test_artist_only_handler_calls_followup_not_play():
    agent, ctrl = _agent()
    bid = agent.bid(_ctx("播放周杰倫"))
    assert bid is not None
    await bid.handler()
    ctrl._ask_music_followup.assert_awaited_once()
    ctrl._safe_music_command.assert_not_awaited()
    # 帶過去的參數至少要有 speaker + query
    args = ctrl._ask_music_followup.await_args
    assert args.args[0] == "Alice"
    assert "周杰倫" in args.args[1]
    # missing_slots 也要帶過去讓 followup 知道缺什麼
    assert "song_title" in args.args[2]


@pytest.mark.asyncio
async def test_complete_query_handler_calls_play_not_followup():
    agent, ctrl = _agent()
    bid = agent.bid(_ctx("播放陶喆的天天"))
    assert bid is not None
    await bid.handler()
    ctrl._safe_music_command.assert_awaited_once()
    ctrl._ask_music_followup.assert_not_awaited()


# ── IntentBus: log 顯示 missing_slots（可觀測）─────────────────────────────

@pytest.mark.asyncio
async def test_bus_log_includes_missing_slots(caplog):
    import logging
    from intent_bus import IntentBus
    agent, _ = _agent()
    bus = IntentBus([agent])
    with caplog.at_level(logging.INFO, logger="cogs.voice_controller.intent_bus"):
        await bus.dispatch(_ctx("播放周杰倫"))
    combined = " ".join(r.message for r in caplog.records)
    assert "song_title" in combined or "missing" in combined.lower(), \
        f"bus log 應該曝光 missing_slots，實際：{combined}"
