"""TDD：`_handle_music_info_query`（IBA Tier 1「這首叫什麼/誰唱的」直答）只送了頻道
文字訊息，沒有語音回覆——8/8 實戰：使用者喚醒後問「這是什麼歌」，log 顯示 query 命中、
reply 文字算出來了、頻道訊息也送了，但完全沒有 TTS，語音上像沒反應。

跟 `_ask_music_followup`（刻意不開 TTS，怕 storm，見該檔 docstring）不同：這裡是
使用者對著麥克風直接問的問題，尤其喚醒路徑（NowPlayingAgent）預期就是要有語音答覆，
文字頻道訊息使用者在語音互動當下不會去看。修法：補上 play_tts，不影響既有頻道訊息。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.router = MagicMock()
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.post_summon_callback = None

    with patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        cog = VoiceController(bot)
    cog.stt_logger = MagicMock()
    cog.play_tts = AsyncMock()
    return cog


@pytest.mark.asyncio
async def test_music_info_query_speaks_the_reply():
    cog = _make_cog()
    cog.active_text_channel = AsyncMock()
    cog._current_stream_info = {
        "title": "左邊的人-陳華 歌詞字幕版", "uploader": "MuSiC CC", "requested_by": "狗與露",
    }

    await cog._handle_music_info_query("狗與露", "這是什麼歌")

    cog.play_tts.assert_awaited_once()
    spoken = cog.play_tts.await_args.args[0]
    assert "左邊的人" in spoken


@pytest.mark.asyncio
async def test_music_info_query_still_sends_channel_message():
    """既有頻道文字訊息行為不能被這次修改動到。"""
    cog = _make_cog()
    cog.active_text_channel = AsyncMock()
    cog._current_stream_info = {"title": "測試曲", "uploader": "", "requested_by": ""}

    await cog._handle_music_info_query("狗與露", "這是什麼歌")

    cog.active_text_channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_music_info_query_no_current_song_does_not_speak():
    cog = _make_cog()
    cog.active_text_channel = AsyncMock()
    cog._current_stream_info = None

    await cog._handle_music_info_query("狗與露", "這是什麼歌")

    cog.play_tts.assert_not_awaited()
    cog.active_text_channel.send.assert_not_awaited()
