"""TDD：MusicAgent — wake 後音樂意圖出價邏輯。

Confidence 規約：
  0.95 — 控制詞 (skip/stop/pause/resume) 或 強訊號 play (「放音樂」「播首歌」)
  0.80 — 弱訊號 play + music marker (「播放陶喆的天天」)
  0.55 — 弱訊號 play + 後續長字串但無 marker (「播放周杰倫」)
  None — 無命中 / 後續是 UI 詞 (「播放控制」) / low confidence wake
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from intent_bus import IntentContext
from intent_agents.music_agent import MusicAgent


def _ctx(query, wake_intent=None, stream_active=False):
    return IntentContext(
        speaker="Alice", raw_text=query, query=query, original_raw=query,
        wake_intent=wake_intent, stream_active=stream_active,
        game_mode=False, is_owner=False, now=100.0,
    )


def _agent():
    """建一個 MusicAgent，掛上真實 kw 列表（從 voice_controller import 避免 drift）。"""
    from cogs.voice_controller import VoiceController as _VC
    ctrl = MagicMock()
    ctrl._STRONG_PLAY_KW   = _VC._STRONG_PLAY_KW
    ctrl._WEAK_PLAY_KW     = _VC._WEAK_PLAY_KW
    ctrl._MUSIC_SKIP_KW    = _VC._MUSIC_SKIP_KW
    ctrl._MUSIC_STOP_KW    = _VC._MUSIC_STOP_KW
    ctrl._MUSIC_PAUSE_KW   = _VC._MUSIC_PAUSE_KW
    ctrl._MUSIC_RESUME_KW  = _VC._MUSIC_RESUME_KW
    ctrl._handle_voice_music_command = MagicMock()
    return MusicAgent(ctrl), ctrl


# ── 強訊號 play → 0.95 ──────────────────────────────────────────────────────

@pytest.mark.parametrize("query", [
    "放音樂",
    "播音樂",
    "放首歌",
    "播首歌",
    "來首老歌",
    "放一首陶喆",
    "搜尋歌曲",
    "play music",
])
def test_strong_play_kw_bids_high(query):
    agent, _ = _agent()
    bid = agent.bid(_ctx(query))
    assert bid is not None
    assert bid.confidence == pytest.approx(0.95)
    assert "strong" in bid.reason.lower() or "play" in bid.reason.lower()


# ── 弱訊號 + music marker → 0.80 ───────────────────────────────────────────

@pytest.mark.parametrize("query", [
    "播放陶喆的天天",         # "的" marker
    "我想聽周杰倫的稻香",      # "的"
    "放點輕音樂",             # "音樂"
    "幫我找一首抒情曲",        # "曲" + "一首"
])
def test_weak_play_with_marker_bids_medium(query):
    agent, _ = _agent()
    bid = agent.bid(_ctx(query))
    assert bid is not None
    assert bid.confidence == pytest.approx(0.80)


# ── 弱訊號 + 長字串無 marker → 0.55 ────────────────────────────────────────

@pytest.mark.parametrize("query", [
    "播放周杰倫",
    "播放Adele",
])
def test_weak_play_artist_only_bids_low(query):
    agent, _ = _agent()
    bid = agent.bid(_ctx(query))
    assert bid is not None
    assert bid.confidence == pytest.approx(0.55)


# ── 弱訊號 + UI 詞 → 不出價（核心 bug case） ──────────────────────────────

@pytest.mark.parametrize("query", [
    "播放控制",   # 5/16 真實 log 誤判過
    "播放清單",
    "播放列表",
    "播放設定",
])
def test_weak_play_blocked_by_ui_target_no_bid(query):
    agent, _ = _agent()
    bid = agent.bid(_ctx(query))
    assert bid is None, f"'{query}' 不該出價"


# ── 控制詞 → 0.95 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("query,expected_cmd", [
    ("換一首", "skip"),
    ("下一首", "skip"),
    ("跳過", "skip"),
    ("停止播放", "stop"),
    ("音樂停", "stop"),
    ("暫停音樂", "pause"),
    ("暫停一下", "pause"),
    ("繼續播", "resume"),
])
def test_control_command_bids_high(query, expected_cmd):
    agent, _ = _agent()
    bid = agent.bid(_ctx(query))
    assert bid is not None
    assert bid.confidence == pytest.approx(0.95)
    assert expected_cmd in bid.reason.lower()


# ── Low-confidence wake → 不出價（不該觸發副作用） ────────────────────────

@pytest.mark.parametrize("wake_intent", [0.30, 0.50, 0.79])
def test_low_confidence_wake_does_not_bid(wake_intent):
    agent, _ = _agent()
    bid = agent.bid(_ctx("播放陶喆的天天", wake_intent=wake_intent))
    assert bid is None


def test_threshold_080_wake_intent_does_bid():
    """wake_intent=0.80 邊界值要會出價。"""
    agent, _ = _agent()
    bid = agent.bid(_ctx("播放陶喆的天天", wake_intent=0.80))
    assert bid is not None


def test_track_a_none_wake_intent_bids_normally():
    """Track A wake_intent=None 視為高信心，正常出價。"""
    agent, _ = _agent()
    bid = agent.bid(_ctx("放音樂", wake_intent=None))
    assert bid is not None
    assert bid.confidence == pytest.approx(0.95)


# ── 無音樂相關 → 不出價 ───────────────────────────────────────────────────

@pytest.mark.parametrize("query", [
    "今天天氣怎樣",
    "你覺得呢",
    "為什麼",
    "",
])
def test_unrelated_query_no_bid(query):
    agent, _ = _agent()
    bid = agent.bid(_ctx(query))
    assert bid is None


# ── Handler 行為：呼叫 controller 帶正確參數 ──────────────────────────────

@pytest.mark.asyncio
async def test_handler_calls_controller_with_play_args():
    agent, ctrl = _agent()
    ctx = _ctx("放音樂")
    # 把 controller 的 method 改成 AsyncMock 讓 bid handler 可以 await
    from unittest.mock import AsyncMock
    ctrl._handle_voice_music_command = AsyncMock()
    bid = agent.bid(ctx)
    assert bid is not None
    await bid.handler()
    ctrl._handle_voice_music_command.assert_awaited_once()
    args = ctrl._handle_voice_music_command.await_args
    # speaker, query, cmd
    assert args.args[0] == "Alice"
    assert args.args[2] == "play"


@pytest.mark.asyncio
async def test_handler_calls_controller_with_control_cmd():
    agent, ctrl = _agent()
    from unittest.mock import AsyncMock
    ctrl._handle_voice_music_command = AsyncMock()
    bid = agent.bid(_ctx("換一首"))
    assert bid is not None
    await bid.handler()
    args = ctrl._handle_voice_music_command.await_args
    assert args.args[2] == "skip"
