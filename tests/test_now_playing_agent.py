"""NowPlayingAgent — 「現在播的是什麼」資訊查詢 intent.

對應 2026-05-27 議題 E #3：L36「現在播的是什麼歌」走 wake 路徑落到 bus 卻無人接，
both-dense-zero。voice_controller 既有 `_MUSIC_INFO_RE` no-wake 直達路徑，本 agent
填 wake gap，patterns 對齊既有 regex（單一語意源）。

confidence 0.90，1 個 intent：now_playing。

mode_compatible = {"normal", "stream"}。
Gate：stream_mode + _current_stream_info 都要存在。

Handler 直接呼叫 ctrl._handle_music_info_query(speaker, query)。
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
        "title": "Test Song",
        "uploader": "Test Artist",
    }
    ctrl._handle_music_info_query = AsyncMock()
    return ctrl


# ── mode gate ─────────────────────────────────────────────────────────────


async def test_game_mode_returns_mode_mismatch():
    from intent_agents.now_playing_agent import NowPlayingAgent
    agent = NowPlayingAgent(_ctrl())
    bid = agent.bid(_ctx("現在播的是什麼歌", mode="game"))
    assert bid.confidence == 0.0
    assert "mode_mismatch" in bid.reason


# ── playback gate ─────────────────────────────────────────────────────────


async def test_no_stream_mode_dense_zero():
    from intent_agents.now_playing_agent import NowPlayingAgent
    agent = NowPlayingAgent(_ctrl(stream_mode=False))
    bid = agent.bid(_ctx("現在播的是什麼歌"))
    assert bid.confidence == 0.0
    assert "stream_not_active" in bid.reason


async def test_no_current_song_dense_zero():
    from intent_agents.now_playing_agent import NowPlayingAgent
    ctrl = _ctrl(stream_mode=True)
    ctrl._current_stream_info = None
    agent = NowPlayingAgent(ctrl)
    bid = agent.bid(_ctx("現在播的是什麼歌"))
    assert bid.confidence == 0.0
    assert "no_current_song" in bid.reason


# ── pattern coverage (對齊 _MUSIC_INFO_RE) ────────────────────────────────


@pytest.mark.parametrize("query", [
    # 「這首…」系列
    "這首歌叫什麼",
    "這首是什麼",
    "這首是誰唱的",  # 「是誰」
    "這首叫做什麼",
    "這首的名字",
    "這首叫啥",
    # 「現在/剛才/正在 …播/放/唱…的」系列（L36 案例）
    "現在播的是什麼歌",  # L36 原案
    "現在播的",
    "剛才放的是什麼",
    "正在唱的是誰",
    # 「歌名/歌手/誰唱」系列
    "歌名叫什麼",
    "歌手是誰",
    "藝人是誰",
    "誰唱的",
    "誰寫的",
])
async def test_now_playing_patterns(query):
    from intent_agents.now_playing_agent import NowPlayingAgent
    agent = NowPlayingAgent(_ctrl())
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.90, f"expected 0.90 for {query!r}, got {bid.confidence}"


# ── no_match ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("query", [
    "今天天氣不錯",
    "播放周杰倫",
    "下一首",
    "把音量調小一點",
])
async def test_no_match_returns_dense_zero(query):
    from intent_agents.now_playing_agent import NowPlayingAgent
    agent = NowPlayingAgent(_ctrl())
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.0


# ── handler integration ───────────────────────────────────────────────────


async def test_handler_calls_music_info_query():
    from intent_agents.now_playing_agent import NowPlayingAgent
    ctrl = _ctrl()
    agent = NowPlayingAgent(ctrl)
    bid = agent.bid(_ctx("現在播的是什麼歌"))
    await bid.handler()
    ctrl._handle_music_info_query.assert_called_once()
    args, _ = ctrl._handle_music_info_query.call_args
    assert args[0] == "alice"  # speaker
    assert "現在播的是什麼歌" in args[1]  # query


async def test_handler_swallows_handle_exception():
    """`_handle_music_info_query` 例外不能炸到 race coordinator。"""
    from intent_agents.now_playing_agent import NowPlayingAgent
    ctrl = _ctrl()
    ctrl._handle_music_info_query = AsyncMock(side_effect=RuntimeError("boom"))
    agent = NowPlayingAgent(ctrl)
    bid = agent.bid(_ctx("現在播的是什麼歌"))
    # 不該 raise
    await bid.handler()


async def test_handler_missing_method_does_not_crash():
    """ctrl 沒裝 _handle_music_info_query 也不該 raise（防 wire 順序問題）。"""
    from intent_agents.now_playing_agent import NowPlayingAgent
    ctrl = _ctrl()
    del ctrl._handle_music_info_query
    agent = NowPlayingAgent(ctrl)
    bid = agent.bid(_ctx("現在播的是什麼歌"))
    await bid.handler()
