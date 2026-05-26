"""ReplayAgent — 重播當前歌曲 intent.

對應 2026-05-27 議題 E #2：L44「重播這一首」是 both-dense-zero 但實際是有效 intent。

confidence 規約：
  0.90 — 重播/再放一次/從頭/倒帶/replay

mode_compatible = {"normal", "stream"}；只在 stream_mode + 有 _current_stream_info
才 bid（radio mode 語意模糊先不做）。

Handler：
  把 _current_stream_info 插回 stream_queue 最前面
  vc.stop_playing() 觸發下一輪 picked up 同一首
  (沿用 prev_button 的 pattern，少 pop history 那段)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_bus import IntentContext


pytestmark = pytest.mark.asyncio


def _ctx(query: str, mode: str = "normal") -> IntentContext:
    return IntentContext(
        speaker="alice",
        raw_text=query,
        query=query,
        original_raw=query,
        wake_intent=0.9,
        stream_active=(mode == "stream"),
        game_mode=(mode == "game"),
        is_owner=False,
        now=0.0,
        mode=mode,
    )


def _ctrl(stream_mode=True, current_info=None):
    ctrl = MagicMock()
    ctrl.stream_mode = stream_mode
    ctrl._current_stream_info = current_info or {
        "url": "https://yt/abc",
        "title": "Test Song",
    }
    ctrl.stream_queue = []
    ctrl.bot = MagicMock()
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.stop_playing = MagicMock()
    vc.stop = MagicMock()
    ctrl.bot.voice_clients = [vc]
    ctrl.play_tts = AsyncMock()
    return ctrl, vc


# ── mode gate ─────────────────────────────────────────────────────────────


async def test_game_mode_returns_mode_mismatch():
    from intent_agents.replay_agent import ReplayAgent
    ctrl, _ = _ctrl()
    agent = ReplayAgent(ctrl)
    bid = agent.bid(_ctx("重播", mode="game"))
    assert bid.confidence == 0.0
    assert "mode_mismatch" in bid.reason


# ── playback gate ─────────────────────────────────────────────────────────


async def test_no_stream_mode_dense_zero():
    from intent_agents.replay_agent import ReplayAgent
    ctrl, _ = _ctrl(stream_mode=False)
    agent = ReplayAgent(ctrl)
    bid = agent.bid(_ctx("重播"))
    assert bid.confidence == 0.0
    assert "stream_not_active" in bid.reason


async def test_no_current_song_dense_zero():
    from intent_agents.replay_agent import ReplayAgent
    ctrl, _ = _ctrl(stream_mode=True)
    ctrl._current_stream_info = None
    agent = ReplayAgent(ctrl)
    bid = agent.bid(_ctx("重播"))
    assert bid.confidence == 0.0
    assert "no_current_song" in bid.reason


# ── replay patterns ───────────────────────────────────────────────────────


@pytest.mark.parametrize("query", [
    "重播",
    "重播這一首",
    "重播這首",
    "再放一次",
    "再播一次",
    "再聽一次",
    "倒回",
    "倒帶",
    "從頭",
    "從頭播",
    "從頭再播",
    "replay",
    "play again",
])
async def test_replay_patterns(query):
    from intent_agents.replay_agent import ReplayAgent
    ctrl, _ = _ctrl()
    agent = ReplayAgent(ctrl)
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.90, f"expected 0.90 for {query!r}, got {bid.confidence}"


# ── 確保不誤觸發既有 intents ────────────────────────────────────────────


@pytest.mark.parametrize("query", [
    "下一首",        # skip_track 的詞
    "再來一首",      # 加歌 != 重播（語意模糊，不接）
    "播放周杰倫",    # music
    "今天天氣不錯",  # 純對話
])
async def test_no_match_avoids_existing_intents(query):
    from intent_agents.replay_agent import ReplayAgent
    ctrl, _ = _ctrl()
    agent = ReplayAgent(ctrl)
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.0, f"{query!r} should not match, got {bid.confidence}"


# ── handler integration ───────────────────────────────────────────────────


async def test_handler_inserts_current_to_queue_front():
    from intent_agents.replay_agent import ReplayAgent
    ctrl, vc = _ctrl()
    ctrl.stream_queue = [{"title": "Next Song", "url": "yt/next"}]
    agent = ReplayAgent(ctrl)
    bid = agent.bid(_ctx("重播"))
    await bid.handler()
    # current 插到 queue[0]，原 Next Song 變 queue[1]
    assert ctrl.stream_queue[0]["title"] == "Test Song"
    assert ctrl.stream_queue[1]["title"] == "Next Song"


async def test_handler_stops_current_playback():
    from intent_agents.replay_agent import ReplayAgent
    ctrl, vc = _ctrl()
    vc.is_playing.return_value = True
    agent = ReplayAgent(ctrl)
    bid = agent.bid(_ctx("重播"))
    await bid.handler()
    # stop_playing 或 stop 之一被呼叫
    assert vc.stop_playing.called or vc.stop.called


async def test_handler_plays_ack():
    from intent_agents.replay_agent import ReplayAgent
    ctrl, _ = _ctrl()
    agent = ReplayAgent(ctrl)
    bid = agent.bid(_ctx("重播"))
    await bid.handler()
    ctrl.play_tts.assert_called_once()


async def test_handler_no_voice_client_does_not_crash():
    """vc 不存在（剛斷線）→ handler 不該 raise，記 log 跳過。"""
    from intent_agents.replay_agent import ReplayAgent
    ctrl, _ = _ctrl()
    ctrl.bot.voice_clients = []  # 沒 vc
    agent = ReplayAgent(ctrl)
    bid = agent.bid(_ctx("重播"))
    # 不該 raise
    await bid.handler()
