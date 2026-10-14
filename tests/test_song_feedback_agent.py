"""SongFeedbackAgent — 控制卡按鈕語音化：誤點刪除 / 喜歡。

對應 2026-10-13 showay「馬文，誤點刪除」被 too_short gate 擋掉，且語音本來沒有
任何路徑能觸發這個按鈕功能。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_agents.song_feedback_agent import SongFeedbackAgent, erase_misclick
from intent_bus import IntentContext

pytestmark = pytest.mark.asyncio


def _ctx(query: str, mode: str = "stream", speaker: str = "showay") -> IntentContext:
    return IntentContext(
        speaker=speaker,
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


def _ctrl(stream_mode=True, cur=None):
    ctrl = MagicMock()
    ctrl.stream_mode = stream_mode
    ctrl._current_stream_info = cur
    ctrl.active_text_channel = MagicMock()
    ctrl.active_text_channel.send = AsyncMock()
    ctrl.play_tts = AsyncMock()
    return ctrl


def _song(**overrides):
    info = {"title": "手滑點到的歌", "uploader": "某藝人",
             "webpage_url": "https://youtu.be/dQw4w9WgXcQ"}
    info.update(overrides)
    return info


# ── regex: misclick_delete ───────────────────────────────────────────────────


@pytest.mark.parametrize("query", [
    "誤刪除", "5. 刪除", "誤點刪除", "刪掉這首", "這首誤點了",
])
async def test_misclick_delete_patterns_match(query):
    agent = SongFeedbackAgent(_ctrl(cur=_song()))
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.92, f"expected 0.92 for {query!r}, got {bid.confidence}"
    assert bid.reason.startswith("misclick:")


@pytest.mark.parametrize("query", [
    "把香菜從我的喜好刪除", "刪除待播清單", "清空待播",
])
async def test_misclick_delete_patterns_no_match(query):
    agent = SongFeedbackAgent(_ctrl(cur=_song()))
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.0


# ── regex: like_song ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("query", [
    "喜歡這首", "按讚", "給這首一個讚",
])
async def test_like_song_patterns_match(query):
    agent = SongFeedbackAgent(_ctrl(cur=_song()))
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.90, f"expected 0.90 for {query!r}, got {bid.confidence}"
    assert bid.reason.startswith("like:")


@pytest.mark.parametrize("query", [
    "我不喜歡這首", "沒有很喜歡這首", "取消按讚",
])
async def test_like_song_patterns_no_match(query):
    agent = SongFeedbackAgent(_ctrl(cur=_song()))
    bid = agent.bid(_ctx(query))
    assert bid.confidence == 0.0


# ── gate ──────────────────────────────────────────────────────────────────────


async def test_gate_stream_not_active():
    agent = SongFeedbackAgent(_ctrl(stream_mode=False, cur=_song()))
    bid = agent.bid(_ctx("誤刪除"))
    assert bid.confidence == 0.0
    assert "stream_not_active" in bid.reason


async def test_gate_no_current_song():
    agent = SongFeedbackAgent(_ctrl(stream_mode=True, cur=None))
    bid = agent.bid(_ctx("誤刪除"))
    assert bid.confidence == 0.0
    assert "no_current_song" in bid.reason


# ── erase_misclick helper ────────────────────────────────────────────────────


def test_erase_misclick_returns_none_when_nothing_playing():
    ctrl = _ctrl(stream_mode=False, cur=None)
    assert erase_misclick(ctrl) is None
    assert not ctrl.bot.music_memory.undo_play.called


def test_erase_misclick_undoes_and_blacklists():
    cur = _song()
    ctrl = _ctrl(stream_mode=True, cur=cur)
    title = erase_misclick(ctrl)
    assert title == "手滑點到的歌"
    ctrl.bot.music_memory.undo_play.assert_called_once_with(cur)
    ctrl.bot.music_memory.record_skipped_video_id.assert_called_once_with(
        "https://youtu.be/dQw4w9WgXcQ")


# ── misclick handler ──────────────────────────────────────────────────────────


async def test_misclick_handler_erases_then_skips_in_order():
    cur = _song()
    ctrl = _ctrl(stream_mode=True, cur=cur)
    mc = MagicMock()
    mc._safe_music_command = AsyncMock()
    ctrl.bot.cogs = {"MusicCog": mc}

    manager = MagicMock()
    manager.attach_mock(ctrl.bot.music_memory.undo_play, "undo_play")
    manager.attach_mock(mc._safe_music_command, "skip")

    agent = SongFeedbackAgent(ctrl)
    bid = agent.bid(_ctx("誤刪除", speaker="showay"))
    await bid.handler()

    ctrl.bot.music_memory.undo_play.assert_called_once_with(cur)
    ctrl.bot.music_memory.record_skipped_video_id.assert_called_once_with(
        "https://youtu.be/dQw4w9WgXcQ")
    mc._safe_music_command.assert_called_once_with("showay", "", "skip")
    call_names = [c[0] for c in manager.mock_calls]
    assert call_names.index("undo_play") < call_names.index("skip"), (
        "erase 必須在 skip 之前做（skip 會換掉 _current_stream_info）")

    sent_text = ctrl.active_text_channel.send.call_args[0][0]
    assert "手滑點到的歌" in sent_text


async def test_misclick_handler_noop_text_when_nothing_playing():
    # gate 擋在 bid()，這裡直接測 handler 本身的 no-song 分支（bypass gate）。
    ctrl = _ctrl(stream_mode=False, cur=None)
    agent = SongFeedbackAgent(ctrl)
    handler = agent._make_misclick_handler("showay")
    await handler()
    ctrl.active_text_channel.send.assert_called_once_with(
        "現在沒有正在播放的歌可以抹除。")
    assert not ctrl.bot.music_memory.undo_play.called


# ── like handler ──────────────────────────────────────────────────────────────


async def test_like_handler_toggles_when_not_yet_liked():
    cur = _song()
    ctrl = _ctrl(stream_mode=True, cur=cur)
    ctrl.bot.music_memory.get_likers.return_value = []
    ctrl.bot.music_memory.toggle_like.return_value = True

    agent = SongFeedbackAgent(ctrl)
    bid = agent.bid(_ctx("喜歡這首", speaker="showay"))
    await bid.handler()

    ctrl.bot.music_memory.toggle_like.assert_called_once_with(cur, "showay")
    sent_text = ctrl.active_text_channel.send.call_args[0][0]
    assert "記下了" in sent_text
    ctrl.play_tts.assert_called_once()


async def test_like_handler_already_liked_skips_toggle():
    cur = _song()
    ctrl = _ctrl(stream_mode=True, cur=cur)
    ctrl.bot.music_memory.get_likers.return_value = ["showay"]

    agent = SongFeedbackAgent(ctrl)
    bid = agent.bid(_ctx("喜歡這首", speaker="showay"))
    await bid.handler()

    assert not ctrl.bot.music_memory.toggle_like.called
    sent_text = ctrl.active_text_channel.send.call_args[0][0]
    assert "已經喜歡" in sent_text
    assert not ctrl.play_tts.called
