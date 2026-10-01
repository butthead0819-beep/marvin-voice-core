"""2026-10-01：「馬文這是什麼歌」報完歌名後疊導聆（歌不中斷）。

三處改動：
1. audiophile_fetcher.strip_entry_cue：導聆稿三幕固定的「進歌引導」尾句切掉。
2. MusicCog.speak_now_playing_guide：查稿 + TTS + 推上 TTS 層 + 貼頻道。
3. NowPlayingAgent._spawn_guide：wake 路徑才背景觸發，nowake 不接。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from intent_bus import IntentContext

# ── strip_entry_cue ─────────────────────────────────────────────────────────


def test_strip_entry_cue_drops_last_sentence():
    from audiophile_fetcher import strip_entry_cue
    text = "破題一句。細節兩句，聽鼓。戴上耳機進歌吧！"
    assert strip_entry_cue(text) == "破題一句。細節兩句，聽鼓。"


def test_strip_entry_cue_keeps_unpunctuated_remainder_as_sentence():
    assert _strip("A。B。C") == "A。B。"


def test_strip_entry_cue_single_sentence_returned_as_is():
    assert _strip("只有一句沒有句點") == "只有一句沒有句點"


def test_strip_entry_cue_empty_string():
    assert _strip("") == ""


def _strip(text):
    from audiophile_fetcher import strip_entry_cue
    return strip_entry_cue(text)


# ── NowPlayingAgent._spawn_guide ─────────────────────────────────────────────


def _ctx(query: str, dispatch_source: str = "regex") -> IntentContext:
    return IntentContext(
        speaker="alice",
        raw_text=query,
        query=query,
        original_raw=query,
        wake_intent=0.9,
        stream_active=True,
        game_mode=False,
        is_owner=False,
        now=0.0,
        mode="stream",
        dispatch_source=dispatch_source,
    )


def _ctrl(mc=None, current_info=None):
    ctrl = MagicMock()
    ctrl.stream_mode = True
    ctrl._current_stream_info = current_info or {"title": "夜曲", "uploader": "周杰倫"}
    ctrl._handle_music_info_query = AsyncMock()
    ctrl.active_text_channel = None
    ctrl._pending_followups = {}
    ctrl.bot = MagicMock()
    ctrl.bot.cogs = {"MusicCog": mc} if mc is not None else {}
    return ctrl


@pytest.mark.asyncio
async def test_wake_path_triggers_speak_now_playing_guide():
    from intent_agents.now_playing_agent import NowPlayingAgent
    mc = MagicMock()
    mc.speak_now_playing_guide = AsyncMock()
    ctrl = _ctrl(mc=mc)
    agent = NowPlayingAgent(ctrl)
    bid = agent.bid(_ctx("這首是誰唱的", dispatch_source="regex"))

    await bid.handler()
    assert agent._guide_task is not None
    await agent._guide_task

    mc.speak_now_playing_guide.assert_awaited_once_with(ctrl._current_stream_info)


@pytest.mark.asyncio
async def test_nowake_path_does_not_trigger_guide():
    from intent_agents.now_playing_agent import NowPlayingAgent
    mc = MagicMock()
    mc.speak_now_playing_guide = AsyncMock()
    ctrl = _ctrl(mc=mc)
    agent = NowPlayingAgent(ctrl)
    bid = agent.bid(_ctx("這是什麼歌", dispatch_source="nowake"))

    await bid.handler()

    mc.speak_now_playing_guide.assert_not_awaited()
    assert agent._guide_task is None


@pytest.mark.asyncio
async def test_does_not_retrigger_while_guide_task_pending():
    from intent_agents.now_playing_agent import NowPlayingAgent
    mc = MagicMock()
    mc.speak_now_playing_guide = AsyncMock()
    ctrl = _ctrl(mc=mc)
    agent = NowPlayingAgent(ctrl)

    async def _never():
        await asyncio.Event().wait()

    pending_task = asyncio.create_task(_never())
    agent._guide_task = pending_task

    bid = agent.bid(_ctx("這首是誰唱的", dispatch_source="regex"))
    await bid.handler()

    mc.speak_now_playing_guide.assert_not_awaited()
    assert agent._guide_task is pending_task

    pending_task.cancel()
    try:
        await pending_task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_missing_music_cog_does_not_crash_and_followup_still_runs():
    from intent_agents.now_playing_agent import NowPlayingAgent
    ctrl = _ctrl(mc=None)  # ctrl.bot.cogs.get("MusicCog") -> None
    ctrl.active_text_channel = AsyncMock()
    agent = NowPlayingAgent(ctrl)
    bid = agent.bid(_ctx("這首是誰唱的", dispatch_source="regex"))

    await bid.handler()  # 不該 raise

    assert agent._guide_task is None
    ctrl.active_text_channel.send.assert_awaited_once()


# ── MusicCog.speak_now_playing_guide ─────────────────────────────────────────


def _make_cog(info=None, vc=None):
    from cogs.music_cog import MusicCog
    cog = MusicCog(MagicMock())
    cog._current_stream_info = info
    cog._vc = MagicMock(return_value=vc)
    store, guard, router = MagicMock(), MagicMock(), MagicMock()
    cog._audiophile_deps = MagicMock(return_value=(store, guard, router))
    return cog


def _make_vc():
    vc = MagicMock()
    vc.play_dj_on_tts_layer = AsyncMock(return_value=True)
    vc._protected_tts_window = MagicMock()
    vc._protected_tts_window.return_value.__enter__ = MagicMock(return_value=None)
    vc._protected_tts_window.return_value.__exit__ = MagicMock(return_value=False)
    vc.active_text_channel = MagicMock()
    vc.active_text_channel.send = AsyncMock()
    return vc


@pytest.mark.asyncio
async def test_speak_now_playing_guide_strips_cue_and_plays_on_tts_layer():
    info = {"title": "夜曲", "uploader": "周杰倫"}
    vc = _make_vc()
    cog = _make_cog(info=info, vc=vc)
    cog._dj_clean_name = MagicMock(return_value=("夜曲", "周杰倫"))
    cog.bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/x.mp3")

    with patch("audiophile_fetcher.song_guide_for_dj", new=AsyncMock(
        return_value="一句。二句。進歌吧！",
    )) as fetch:
        await cog.speak_now_playing_guide(info)

    cog.bot.tts_engine.generate_audio.assert_awaited_once_with("一句。二句。")
    vc.play_dj_on_tts_layer.assert_awaited_once_with("/tmp/x.mp3", text="一句。二句。")
    vc.active_text_channel.send.assert_awaited_once()
    assert "一句。二句。" in vc.active_text_channel.send.await_args.args[0]
    assert fetch.await_args.kwargs["human"] is True


@pytest.mark.asyncio
async def test_speak_now_playing_guide_uses_canon_label():
    info = {"title": "Tian Tian", "uploader": "x", "_canon": {"title": "天天", "artist": "陶喆"}}
    vc = _make_vc()
    cog = _make_cog(info=info, vc=vc)
    cog.bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/x.mp3")

    with patch("audiophile_fetcher.song_guide_for_dj", new=AsyncMock(
        return_value="一句。二句。進歌吧！",
    )) as fetch:
        await cog.speak_now_playing_guide(info)

    assert fetch.await_args.args[0] == "陶喆 - 天天"


@pytest.mark.asyncio
async def test_speak_now_playing_guide_no_guide_skips_tts():
    info = {"title": "夜曲", "uploader": "周杰倫"}
    vc = _make_vc()
    cog = _make_cog(info=info, vc=vc)
    cog._dj_clean_name = MagicMock(return_value=("夜曲", "周杰倫"))
    cog.bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/x.mp3")

    with patch("audiophile_fetcher.song_guide_for_dj", new=AsyncMock(return_value=None)):
        await cog.speak_now_playing_guide(info)

    cog.bot.tts_engine.generate_audio.assert_not_awaited()
    vc.play_dj_on_tts_layer.assert_not_awaited()


@pytest.mark.asyncio
async def test_speak_now_playing_guide_discards_if_song_changed_during_fetch():
    info = {"title": "夜曲", "uploader": "周杰倫"}
    vc = _make_vc()
    cog = _make_cog(info=info, vc=vc)
    cog._dj_clean_name = MagicMock(return_value=("夜曲", "周杰倫"))
    cog.bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/x.mp3")

    async def _fetch_then_swap(*args, **kwargs):
        cog._current_stream_info = {"title": "別的歌"}
        return "一句。二句。進歌吧！"

    with patch("audiophile_fetcher.song_guide_for_dj", new=AsyncMock(side_effect=_fetch_then_swap)):
        await cog.speak_now_playing_guide(info)

    cog.bot.tts_engine.generate_audio.assert_not_awaited()
    vc.play_dj_on_tts_layer.assert_not_awaited()


@pytest.mark.asyncio
async def test_speak_now_playing_guide_no_vc_returns_early():
    info = {"title": "夜曲", "uploader": "周杰倫"}
    cog = _make_cog(info=info, vc=None)

    with patch("audiophile_fetcher.song_guide_for_dj", new=AsyncMock()) as fetch:
        await cog.speak_now_playing_guide(info)

    fetch.assert_not_awaited()
