"""TDD — 10/8 使用者定案：mode=="activity" 時 DJ 串場 ctx 包含在場者的 Discord 動態。

沿用 tests/test_dj_callback.py 的 harness（同一支 `_fetch_dj_interjection_raw`，
同一種 mock 方式），只是這次驗證 activity 這條 mode 的 ctx 內容。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cogs.music_cog import MusicCog
from dj_topic_selector import TopicCooldownStore


@pytest.mark.asyncio
async def test_dj_interjection_activity_context(tmp_path):
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/dj_audio.opus")
    bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)
    bot.router = MagicMock()
    bot.router.generate_dynamic_system_msg = AsyncMock(return_value="邊打遊戲邊聽這首吧")
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    bot.engine.post_summon_callback = None
    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="song_key_xyz")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="午後")

    cog = MusicCog(bot)
    cog._enable_dj_news_fetch = False
    cog._dj_topic_cooldown_store = TopicCooldownStore(path=str(tmp_path / "cd.json"))

    with patch.object(cog, "_dj_clean_name", return_value=("歌曲", "歌手")), \
         patch(
             "dj_narration_orchestrator.select_narration_mode",
             return_value=("小明 正在玩《Ball X Pit》", "activity"),
         ):
        info = {"title": "歌曲 - 歌手", "uploader": "歌手", "requested_by": "Alice", "url": "https://example/x"}
        await cog._fetch_dj_interjection_raw(info)

    call = bot.router.generate_dynamic_system_msg.call_args
    assert call is not None
    ctx = call.kwargs.get("context", "")
    assert "【你熟悉他的生活】在場的人現在的 Discord 動態：" in ctx
    assert "小明 正在玩《Ball X Pit》" in ctx
    assert "開場鉤子：像注意到朋友正在幹嘛順口一提" in ctx


@pytest.mark.asyncio
async def test_dj_interjection_feeds_voice_members_activity_into_gacha(tmp_path):
    """呼叫端接線：語音頻道在場成員的「正在玩」要真的進到 select_narration_mode 的 activities。"""
    from types import SimpleNamespace

    import discord

    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/dj_audio.opus")
    bot.tts_engine.get_estimated_duration = MagicMock(return_value=3.0)
    bot.router = MagicMock()
    bot.router.generate_dynamic_system_msg = AsyncMock(return_value="接下一首")
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    bot.engine.post_summon_callback = None
    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="song_key_xyz")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="午後")

    cog = MusicCog(bot)
    cog._enable_dj_news_fetch = False
    cog._dj_topic_cooldown_store = TopicCooldownStore(path=str(tmp_path / "cd.json"))
    member = SimpleNamespace(
        display_name="狗與露", bot=False,
        activities=[SimpleNamespace(type=discord.ActivityType.playing, name="Ball X Pit")],
    )
    vc = MagicMock()
    vc.voice_client = SimpleNamespace(channel=SimpleNamespace(members=[member]))
    vc.get_online_members = MagicMock(return_value=["狗與露"])
    cog._vc = MagicMock(return_value=vc)

    selector = MagicMock(return_value=(None, "quick"))
    with patch.object(cog, "_dj_clean_name", return_value=("歌曲", "歌手")), \
         patch("dj_narration_orchestrator.select_narration_mode", selector):
        info = {"title": "歌曲 - 歌手", "uploader": "歌手", "requested_by": "Alice", "url": "https://example/x"}
        await cog._fetch_dj_interjection_raw(info)

    assert selector.called
    assert selector.call_args.kwargs["activities"] == ["狗與露 正在玩《Ball X Pit》"]
