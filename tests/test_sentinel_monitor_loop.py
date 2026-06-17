"""
TDD：Sentinel monitor loop 不應在馬文播放音訊時誤觸發 soft_repair。

情境：使用者一人在頻道、不講話、只聽馬文播 YouTube 音樂。
sink.last_decrypted_audio_time 自然會超過 300s 閾值，但 Marvin 自己還在
play() 表示連線是健康的，不該 disconnect+reconnect 中斷音樂。
"""
from __future__ import annotations

import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


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
    cog.active_text_channel = AsyncMock()
    cog.is_recovering = False
    cog.radio_mode = False
    cog.stream_mode = False
    cog.is_playing_audio = False
    cog.soft_repair_count = 0
    cog.sink_missing_count = 0
    cog.dave_error_count = 0
    cog.connection_time = time.time() - 200  # 過寬限期 (>30s) 但未過 reset 線 (<120s)
    cog.last_failure_time = 0
    cog.soft_repair_connection = AsyncMock()
    cog.self_restart = AsyncMock()
    cog._mixer = MagicMock()
    cog._mixer.is_idle.return_value = True
    cog._mixer.is_playing_audio = False
    return cog


def _make_vc_with_member():
    """Voice client with one non-bot member who is not self-muted."""
    member = MagicMock()
    member.bot = False
    member.voice = MagicMock()
    member.voice.self_mute = False

    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = False
    vc.channel = MagicMock()
    vc.channel.members = [member]
    return vc


def _make_sink(silence_seconds: float):
    sink = MagicMock()
    sink.last_decrypted_audio_time = time.time() - silence_seconds
    sink.last_audio_packet_time = time.time() - silence_seconds
    return sink


@pytest.mark.asyncio
async def test_sentinel_triggers_soft_repair_when_silent_and_not_playing():
    """Baseline：靜默 >300s 且馬文沒在播放 → 應該觸發 soft_repair（既有行為）"""
    cog = _make_cog()
    vc = _make_vc_with_member()
    cog.bot.voice_clients = [vc]
    cog.bot.engine.get_active_sink.return_value = _make_sink(silence_seconds=350)
    cog.is_playing_audio = False

    await cog.sentinel_monitor_loop.coro(cog)

    cog.soft_repair_connection.assert_awaited_once()


@pytest.mark.asyncio
async def test_sentinel_skips_soft_repair_when_playing_audio():
    """Bug 1：馬文 is_playing_audio=True 時，即使 silence>300s 也不該 soft_repair。

    理由：Marvin 還在 play() 代表 voice connection 健康，使用者只是在聽音樂。
    """
    cog = _make_cog()
    vc = _make_vc_with_member()
    cog.bot.voice_clients = [vc]
    cog.bot.engine.get_active_sink.return_value = _make_sink(silence_seconds=400)
    cog.is_playing_audio = True
    cog._mixer.is_playing_audio = True

    await cog.sentinel_monitor_loop.coro(cog)

    cog.soft_repair_connection.assert_not_awaited()


@pytest.mark.asyncio
async def test_sentinel_skips_soft_repair_when_stream_mode():
    """stream_mode（Twitch 串流模式）時也應跳過 soft_repair。"""
    cog = _make_cog()
    vc = _make_vc_with_member()
    cog.bot.voice_clients = [vc]
    cog.bot.engine.get_active_sink.return_value = _make_sink(silence_seconds=400)
    cog.stream_mode = True

    await cog.sentinel_monitor_loop.coro(cog)

    cog.soft_repair_connection.assert_not_awaited()


@pytest.mark.asyncio
async def test_sentinel_skips_soft_repair_when_voice_client_playing():
    """voice_client.is_playing()=True（純底層播放中）也應跳過。"""
    cog = _make_cog()
    vc = _make_vc_with_member()
    vc.is_playing.return_value = True
    cog.bot.voice_clients = [vc]
    cog.bot.engine.get_active_sink.return_value = _make_sink(silence_seconds=400)
    cog.is_playing_audio = False  # 旗標沒設，但底層 is_playing 為 True

    await cog.sentinel_monitor_loop.coro(cog)

    cog.soft_repair_connection.assert_not_awaited()


@pytest.mark.asyncio
async def test_sentinel_still_repairs_on_silence_when_not_playing_in_radio_mode():
    """radio_mode 既有行為不變：閾值 720s。低於 720s 不觸發。"""
    cog = _make_cog()
    vc = _make_vc_with_member()
    cog.bot.voice_clients = [vc]
    cog.bot.engine.get_active_sink.return_value = _make_sink(silence_seconds=600)
    cog.radio_mode = True
    cog.is_playing_audio = False

    await cog.sentinel_monitor_loop.coro(cog)

    cog.soft_repair_connection.assert_not_awaited()
