"""Bug 1: NemoClaw ack 音效播放必須走 playback_lock。

舊版直接 `_vc.play(FFmpegPCMAudio(...))` 沒經過 playback_lock，違反
CLAUDE.md 規範「playback_lock → 序列化所有 voice_client.play()」。
後續 TTS 串流要播放時呼叫 stop() 把 ack 的 ffmpeg subprocess SIGTERM 掉，
log 噴 FFmpegProcessError code 245。

修法：把 ack play 抽成 `_play_nemoclaw_ack(speaker)`，內部 acquire playback_lock。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


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

    cog._speaker_lang = {}
    return cog


@pytest.mark.asyncio
async def test_play_nemoclaw_ack_acquires_playback_lock(tmp_path):
    """ack 播放必須在 playback_lock 內，且確實呼叫 _vc.play() 一次。"""
    cog = _make_cog()
    fake_vc = MagicMock()
    fake_vc.is_connected.return_value = True
    fake_vc.is_playing.return_value = False
    fake_vc.play = MagicMock()
    cog.bot.voice_clients = [fake_vc]

    # 用一個會抓到「lock 是否被持有」的監視器
    lock_held_during_play = {"value": False}
    real_play = fake_vc.play

    def _spy_play(*args, **kwargs):
        lock_held_during_play["value"] = cog.playback_lock.locked()
        return real_play(*args, **kwargs)

    fake_vc.play = MagicMock(side_effect=_spy_play)

    ack_file = tmp_path / "ack_1.mp3"
    ack_file.write_bytes(b"fake-mp3")

    with patch("glob.glob", return_value=[str(ack_file)]), \
         patch("discord.FFmpegPCMAudio", return_value=MagicMock()):
        await cog._play_nemoclaw_ack("狗與露")

    assert fake_vc.play.called, "_play_nemoclaw_ack 應該呼叫 voice_client.play()"
    assert lock_held_during_play["value"] is True, (
        "voice_client.play() 必須在 playback_lock 持有期間執行，"
        "否則後續 TTS stop() 會把 ack 的 ffmpeg SIGTERM 掉"
    )


@pytest.mark.asyncio
async def test_play_nemoclaw_ack_skips_when_already_playing():
    """若 vc 正在播放（例如其他 TTS），不應強行插入 ack。"""
    cog = _make_cog()
    fake_vc = MagicMock()
    fake_vc.is_connected.return_value = True
    fake_vc.is_playing.return_value = True
    fake_vc.play = MagicMock()
    cog.bot.voice_clients = [fake_vc]

    await cog._play_nemoclaw_ack("狗與露")
    assert not fake_vc.play.called, "vc 正在播放時不應插隊"


@pytest.mark.asyncio
async def test_play_nemoclaw_ack_handles_no_voice_client():
    """無 voice client 時靜默跳過，不噴錯。"""
    cog = _make_cog()
    cog.bot.voice_clients = []
    await cog._play_nemoclaw_ack("狗與露")  # 不能 raise


@pytest.mark.asyncio
async def test_play_nemoclaw_ack_handles_missing_ack_dir(tmp_path):
    """ack 目錄找不到檔案時靜默跳過。"""
    cog = _make_cog()
    fake_vc = MagicMock()
    fake_vc.is_connected.return_value = True
    fake_vc.is_playing.return_value = False
    fake_vc.play = MagicMock()
    cog.bot.voice_clients = [fake_vc]

    with patch("glob.glob", return_value=[]):
        await cog._play_nemoclaw_ack("狗與露")

    assert not fake_vc.play.called
