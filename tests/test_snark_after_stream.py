"""
stop_stream() 和 stream loop 結束後必須重置 last_marvin_speech_time，
否則音樂播完後的第一句話就會觸發幾十分鐘的「沉默」嘲諷。
"""
from __future__ import annotations
import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None

    with patch("discord_voice_engine.faster_whisper", None, create=True):
        from discord_voice_engine import DiscordVoiceEngine
        engine = DiscordVoiceEngine(bot)
    bot.engine = engine

    with patch("discord.ext.tasks.loop", lambda *a, **kw: lambda f: f), \
         patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        cog = VoiceController(bot)

    cog.stream_mode = True
    cog.stream_task = None
    cog._radio_fade_task = None
    cog._radio_source = None
    cog._current_stream_info = None
    cog.stream_paused = False
    return cog


@pytest.mark.asyncio
async def test_stop_stream_resets_last_marvin_speech_time():
    """stop_stream() 後 last_marvin_speech_time 應更新為現在。"""
    cog = _make_cog()
    old_time = time.time() - 3000  # 模擬 50 分鐘前
    cog.last_marvin_speech_time = old_time

    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = False
    cog.bot.voice_clients = [vc]

    before = time.time()
    await cog.stop_stream(reason="test")
    after = time.time()

    assert cog.last_marvin_speech_time >= before
    assert cog.last_marvin_speech_time <= after


@pytest.mark.asyncio
async def test_stop_stream_does_not_reset_if_not_in_stream_mode():
    """非串流狀態呼叫 stop_stream() 不應改動 last_marvin_speech_time。"""
    cog = _make_cog()
    cog.stream_mode = False
    old_time = time.time() - 3000
    cog.last_marvin_speech_time = old_time

    await cog.stop_stream(reason="test")

    assert cog.last_marvin_speech_time == old_time
