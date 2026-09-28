"""TDD：/guide_song 斜線指令（docs/PLAN_audiophile_music_tour.md Phase 4.1）。

設計：指令內先 await 抓稿 + TTS 預渲染（interaction 已 defer，等十幾秒 OK），渲染完才
入隊——佇列空時歌會立刻開播，背景渲染會來不及，導聆就白做了。
入隊走 _queue_user_song（跟 /marvin_play 同一入口：dedup/ledger/從頭播全共用）。
SongKnowledgeStore 用 cog 共用的 _song_knowledge_store（多實例整份寫檔會互蓋）。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from discord import app_commands


def _make_interaction(username="狗與露", in_voice=True):
    inter = MagicMock()
    inter.user.display_name = username
    inter.response.defer = AsyncMock()
    msg = MagicMock()
    msg.edit = AsyncMock()
    inter.followup.send = AsyncMock(return_value=msg)
    inter.guild.voice_client = MagicMock() if in_voice else None
    return inter, msg


def _make_cog(events):
    from cogs.music_cog import MusicCog
    bot = MagicMock()
    cog = MusicCog(bot)
    cog.radio_mode = False
    cog._vc = MagicMock(return_value=MagicMock())
    info = {"title": "周杰倫 Jay Chou【雙截棍】", "url": "https://ex/cdn",
            "webpage_url": "https://youtube.com/watch?v=abc"}
    cog._resolve_yt_query = AsyncMock(return_value=info)

    async def _prep(i):
        events.append("prepare")
        i.update({"_audiophile_guide": True, "_audiophile_guide_text": "導聆台詞",
                  "_audiophile_guide_audio": "/tmp/g.mp3", "_audiophile_guide_dur": 20.0})

    cog._prepare_audiophile_guide = AsyncMock(side_effect=_prep)
    cog._queue_user_song = MagicMock(side_effect=lambda i, **kw: events.append("queue"))
    cog._ensure_stream_loop = MagicMock(side_effect=lambda: events.append("ensure"))
    return cog, info


def test_guide_song_registered_in_commands_mixin():
    from cogs.music_cog import MusicCog
    cmd = getattr(MusicCog, "guide_song")
    assert isinstance(cmd, app_commands.Command)
    assert cmd.name == "guide_song"
    assert cmd.callback.__module__ == "cogs.music_cog_commands"


@pytest.mark.asyncio
async def test_guide_song_prepares_guide_before_queueing():
    events = []
    cog, info = _make_cog(events)
    inter, msg = _make_interaction("狗與露")

    await cog.guide_song.callback(cog, inter, song_name="周杰倫 雙截棍")

    inter.response.defer.assert_awaited_once()
    cog._resolve_yt_query.assert_awaited_once_with("周杰倫 雙截棍")
    cog._prepare_audiophile_guide.assert_awaited_once_with(info)
    assert events == ["prepare", "queue", "ensure"]
    queued = cog._queue_user_song.call_args.args[0]
    assert queued is info
    assert queued["requested_by"] == "狗與露"
    assert queued["_audiophile_guide"] is True
    # 回報訊息帶導聆台詞
    assert "導聆台詞" in msg.edit.call_args.kwargs["content"]


@pytest.mark.asyncio
async def test_guide_song_not_found_does_not_queue():
    events = []
    cog, _ = _make_cog(events)
    cog._resolve_yt_query = AsyncMock(return_value=None)
    inter, msg = _make_interaction()

    await cog.guide_song.callback(cog, inter, song_name="不存在的歌")

    cog._prepare_audiophile_guide.assert_not_awaited()
    cog._queue_user_song.assert_not_called()
    assert "找不到" in msg.edit.call_args.kwargs["content"]


@pytest.mark.asyncio
async def test_guide_song_requires_bot_in_voice():
    events = []
    cog, _ = _make_cog(events)
    inter, _ = _make_interaction(in_voice=False)

    await cog.guide_song.callback(cog, inter, song_name="周杰倫 雙截棍")

    cog._resolve_yt_query.assert_not_awaited()
    cog._queue_user_song.assert_not_called()


@pytest.mark.asyncio
async def test_guide_song_stops_radio_before_queueing():
    events = []
    cog, _ = _make_cog(events)
    cog.radio_mode = True
    cog.stop_radio = AsyncMock(side_effect=lambda **kw: events.append("stop_radio"))
    inter, _ = _make_interaction()

    await cog.guide_song.callback(cog, inter, song_name="周杰倫 雙截棍")

    assert events.index("stop_radio") < events.index("queue")


@pytest.mark.asyncio
async def test_prepare_audiophile_guide_wires_shared_deps():
    """_prepare_audiophile_guide 接線：共用 store、router 的 free/paid client、
    bot.tts_engine、_probe_audio_duration、_dj_clean_name 乾淨歌名。"""
    from cogs.music_cog import MusicCog
    bot = MagicMock()
    cog = MusicCog(bot)
    shared_store = MagicMock()
    cog._song_knowledge_store = shared_store
    cog._dj_clean_name = MagicMock(return_value=("雙截棍", "周杰倫"))
    info = {"title": "周杰倫 Jay Chou【雙截棍】"}

    with patch("audiophile_fetcher.render_audiophile_guide", new=AsyncMock()) as render:
        await cog._prepare_audiophile_guide(info)

    render.assert_awaited_once()
    args, kw = render.await_args.args, render.await_args.kwargs
    assert args[0] is info
    assert kw["title"] == "雙截棍" and kw["artist"] == "周杰倫"
    assert kw["store"] is shared_store
    assert kw["free_client"] is bot.router.google_client
    assert kw["paid_client"] is bot.router.google_paid_client
    assert kw["tts_engine"] is bot.tts_engine
    assert kw["probe_duration"] == cog._probe_audio_duration
    assert kw["guard"] is not None


@pytest.mark.asyncio
async def test_prepare_audiophile_guide_creates_and_keeps_store_when_missing():
    from cogs.music_cog import MusicCog
    from song_knowledge_store import SongKnowledgeStore
    cog = MusicCog(MagicMock())
    cog._dj_clean_name = MagicMock(return_value=("雙截棍", ""))
    if hasattr(cog, "_song_knowledge_store"):
        del cog._song_knowledge_store

    with patch("audiophile_fetcher.render_audiophile_guide", new=AsyncMock()) as render, \
         patch("song_knowledge_store.SongKnowledgeStore._load", return_value={}):
        await cog._prepare_audiophile_guide({"title": "x"})
        await cog._prepare_audiophile_guide({"title": "y"})

    s1 = render.await_args_list[0].kwargs["store"]
    s2 = render.await_args_list[1].kwargs["store"]
    assert isinstance(s1, SongKnowledgeStore)
    assert s1 is s2 is cog._song_knowledge_store
