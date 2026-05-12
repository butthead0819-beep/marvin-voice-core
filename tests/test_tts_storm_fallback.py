"""
TTS Storm Fallback — 播放中或佇列滿時改貼文，不排隊。

Rules:
  1. is_playing_audio=True + not _tts_protected → drop + post text
  2. tts_queue_duration > threshold + not _tts_protected → drop + post text
  3. already_in_channel=True → text 已由呼叫方貼出，drop 時不重複貼
  4. _tts_protected=True → 不受 storm guard 影響（進場台詞、遊戲線索等）
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.get_estimated_duration.return_value = 2.0

    with patch("discord_voice_engine.faster_whisper", None, create=True):
        from discord_voice_engine import DiscordVoiceEngine
        engine = DiscordVoiceEngine(bot)
    bot.engine = engine

    with patch("discord.ext.tasks.loop", lambda *a, **kw: lambda f: f), \
         patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        cog = VoiceController(bot)

    cog.active_text_channel = AsyncMock()
    cog.active_text_channel.send = AsyncMock()
    cog.game_mode = False
    cog._tts_protected = False
    cog._tts_interrupted = False
    cog._tts_flush_requested = False
    cog.stream_mode = False
    cog.radio_mode = False
    cog.is_playing_audio = False
    cog.tts_queue_duration = 0.0
    return cog


@pytest.mark.asyncio
async def test_storm_guard_posts_text_when_already_playing():
    """is_playing_audio=True → TTS 放棄，改貼文到頻道。"""
    cog = _make_cog()
    cog.is_playing_audio = True

    with patch("os.mkfifo"), patch("tempfile.mkdtemp", return_value="/tmp/x"):
        await cog.play_tts("你好馬文", already_in_channel=False)

    cog.active_text_channel.send.assert_called_once()
    sent_text = cog.active_text_channel.send.call_args[0][0]
    assert "你好馬文" in sent_text


@pytest.mark.asyncio
async def test_storm_guard_skips_post_when_already_in_channel():
    """is_playing_audio=True + already_in_channel=True → 不重複貼（呼叫方已貼）。"""
    cog = _make_cog()
    cog.is_playing_audio = True

    with patch("os.mkfifo"), patch("tempfile.mkdtemp", return_value="/tmp/x"):
        await cog.play_tts("你好馬文", already_in_channel=True)

    cog.active_text_channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_protected_tts_bypasses_storm_guard():
    """_tts_protected=True → storm guard 不干預（進場台詞不能被截掉）。"""
    cog = _make_cog()
    cog.is_playing_audio = True
    cog._tts_protected = True

    fifo_created = []

    with patch("os.mkfifo", side_effect=lambda p: fifo_created.append(p)), \
         patch("tempfile.mkdtemp", return_value="/tmp/x"):
        try:
            await cog.play_tts("不可中斷台詞", already_in_channel=True)
        except Exception:
            pass  # VoiceClient 找不到，但 FIFO 已建立代表有通過 storm guard

    # 沒有貼文（protected 不改貼文）
    cog.active_text_channel.send.assert_not_called()
    # FIFO 有被建立，代表有進到播放邏輯
    assert len(fifo_created) > 0


@pytest.mark.asyncio
async def test_queue_overflow_posts_text():
    """tts_queue_duration > threshold → 改貼文，不排隊。"""
    cog = _make_cog()
    cog.is_playing_audio = False
    cog.tts_queue_duration = 10.0  # 超過 RESPONSE(1) 的 8.0s 閾值

    with patch("os.mkfifo"), patch("tempfile.mkdtemp", return_value="/tmp/x"):
        await cog.play_tts("佇列已滿這句話", already_in_channel=False)

    cog.active_text_channel.send.assert_called_once()
    sent_text = cog.active_text_channel.send.call_args[0][0]
    assert "佇列已滿這句話" in sent_text


@pytest.mark.asyncio
async def test_queue_overflow_no_duplicate_when_already_in_channel():
    """tts_queue_duration > threshold + already_in_channel=True → 不重複貼。"""
    cog = _make_cog()
    cog.tts_queue_duration = 10.0

    with patch("os.mkfifo"), patch("tempfile.mkdtemp", return_value="/tmp/x"):
        await cog.play_tts("已在頻道的文字", already_in_channel=True)

    cog.active_text_channel.send.assert_not_called()
