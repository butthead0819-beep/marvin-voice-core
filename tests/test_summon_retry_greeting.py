"""
Summon retry path must trigger post_summon_callback (BOT降臨).

Bug: when channel.connect() times out and the retry succeeds, the retry
path never calls post_summon_callback — so BOT降臨 greeting is skipped.
"""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_bot_and_engine():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.router.generate_greeting = AsyncMock(return_value="")

    with patch("discord_voice_engine.faster_whisper", None, create=True):
        from discord_voice_engine import DiscordVoiceEngine
        engine = DiscordVoiceEngine(bot)
    bot.engine = engine
    engine.text_channel_callback = None
    engine.post_summon_callback = None
    return bot, engine


def _make_cog(bot):
    """Instantiate VoiceController with a mock bot."""
    with patch("discord.ext.tasks.loop", lambda *a, **kw: lambda f: f), \
         patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        cog = VoiceController(bot)
    return cog


def _make_interaction(channel):
    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.user.voice.channel = channel
    interaction.guild.voice_client = None
    interaction.channel = MagicMock()
    return interaction


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_summon_calls_post_summon_callback():
    """Normal (non-retry) summon must call post_summon_callback."""
    bot, engine = _make_bot_and_engine()
    cog = _make_cog(bot)

    callback = AsyncMock()
    engine.post_summon_callback = callback

    voice_client = MagicMock()
    voice_client.is_connected.return_value = True
    voice_client.is_playing.return_value = False

    channel = MagicMock()
    channel.name = "語音"
    channel.connect = AsyncMock(return_value=voice_client)

    interaction = _make_interaction(channel)

    with patch("discord_voice_engine.RealtimeVADSink", MagicMock()), \
         patch("discord_voice_engine.patch_voice_recv_key_sync", MagicMock()), \
         patch("cogs.voice_controller.voice_recv", MagicMock()):
        await cog.summon.callback(cog, interaction)

    await asyncio.sleep(0)  # let create_task fire
    callback.assert_called_once()


@pytest.mark.asyncio
async def test_summon_retry_calls_post_summon_callback():
    """
    When first connect() raises (simulating UDP timeout) and retry succeeds,
    post_summon_callback must still be called so BOT降臨 is triggered.
    """
    bot, engine = _make_bot_and_engine()
    cog = _make_cog(bot)

    callback = AsyncMock()
    engine.post_summon_callback = callback

    voice_client = MagicMock()
    voice_client.is_connected.return_value = True
    voice_client.is_playing.return_value = False

    channel = MagicMock()
    channel.name = "語音"
    # First call raises, second call succeeds
    channel.connect = AsyncMock(
        side_effect=[TimeoutError("UDP timeout"), voice_client]
    )

    interaction = _make_interaction(channel)

    with patch("discord_voice_engine.RealtimeVADSink", MagicMock()), \
         patch("discord_voice_engine.patch_voice_recv_key_sync", MagicMock()), \
         patch("cogs.voice_controller.voice_recv", MagicMock()), \
         patch("asyncio.sleep", AsyncMock()):  # skip the 2s retry delay
        await cog.summon.callback(cog, interaction)

    await asyncio.sleep(0)
    callback.assert_called_once()


@pytest.mark.asyncio
async def test_summon_retry_no_crash_when_callback_is_none():
    """If post_summon_callback is None, retry path must not crash."""
    bot, engine = _make_bot_and_engine()
    cog = _make_cog(bot)
    engine.post_summon_callback = None

    voice_client = MagicMock()
    voice_client.is_connected.return_value = True

    channel = MagicMock()
    channel.name = "語音"
    channel.connect = AsyncMock(
        side_effect=[TimeoutError("UDP timeout"), voice_client]
    )

    interaction = _make_interaction(channel)

    with patch("discord_voice_engine.RealtimeVADSink", MagicMock()), \
         patch("discord_voice_engine.patch_voice_recv_key_sync", MagicMock()), \
         patch("cogs.voice_controller.voice_recv", MagicMock()), \
         patch("asyncio.sleep", AsyncMock()):
        await cog.summon.callback(cog, interaction)  # should not raise
