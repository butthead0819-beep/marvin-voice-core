"""
TTS Mixer Queue Fallback (Plan 12) — 本地混音台佇列滿時改貼文，不疊播。

Rules:
  1. mixer.tts_load_seconds() > threshold + not _tts_protected → drop + post text
  2. already_in_channel=True → text 已由呼叫方貼出，drop 時不重複貼
  3. _tts_protected=True → 不受佇列長度影響
"""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.get_estimated_duration.return_value = 2.0
    vc = MagicMock()
    vc.is_connected.return_value = True
    bot.voice_clients = [vc]

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

    # Mock mixer
    cog._mixer = MagicMock()
    cog._mixer.tts_load_seconds.return_value = 0.0
    cog._stream_tts_to_mixer = AsyncMock()
    cog._ensure_mixer_playing = MagicMock()
    return cog


@pytest.mark.asyncio
async def test_queue_overflow_posts_text_when_exceeding_threshold():
    """mixer.tts_load_seconds() > 8.0 (RESPONSE threshold) → TTS 放棄，改貼文。"""
    cog = _make_cog()
    cog._mixer.tts_load_seconds.return_value = 10.0  # 超過 RESPONSE(1) 的 8.0s 閾值

    await cog.play_tts("佇列已滿這句話", priority=1, already_in_channel=False)

    cog.active_text_channel.send.assert_called_once()
    sent_text = cog.active_text_channel.send.call_args[0][0]
    assert "佇列已滿這句話" in sent_text
    cog._stream_tts_to_mixer.assert_not_called()


@pytest.mark.asyncio
async def test_queue_overflow_skips_post_when_already_in_channel():
    """mixer.tts_load_seconds() > 8.0 + already_in_channel=True → 放棄播放且不重複貼文。"""
    cog = _make_cog()
    cog._mixer.tts_load_seconds.return_value = 10.0

    await cog.play_tts("佇列已滿這句話", priority=1, already_in_channel=True)

    cog.active_text_channel.send.assert_not_called()
    cog._stream_tts_to_mixer.assert_not_called()


@pytest.mark.asyncio
async def test_protected_tts_bypasses_queue_overflow():
    """_tts_protected=True → 佇列滿也照樣播放。"""
    cog = _make_cog()
    cog._mixer.tts_load_seconds.return_value = 10.0
    cog._tts_protected = True

    await cog.play_tts("不可中斷台詞", priority=1, already_in_channel=False)

    cog.active_text_channel.send.assert_not_called()
    cog._stream_tts_to_mixer.assert_called_once()


@pytest.mark.asyncio
async def test_tts_plays_normally_when_below_threshold():
    """佇列長度未超標 → 正常播放。"""
    cog = _make_cog()
    cog._mixer.tts_load_seconds.return_value = 2.0  # 未超標

    await cog.play_tts("正常語音內容", priority=1, already_in_channel=False)

    cog.active_text_channel.send.assert_not_called()
    cog._stream_tts_to_mixer.assert_called_once()
