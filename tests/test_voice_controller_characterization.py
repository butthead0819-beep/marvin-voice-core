"""Characterization (golden snapshot) tests on the voice_controller god-class.

目的：voice_controller.py 是 7857 LOC、0 test 的播放核心，且 Plan 12（本地混音台）
即將整個重寫此播放路徑。先對「現有可觀察契約」拍快照，作為 Plan 12 mixer 必須
保住的行為基線（重寫後對照）。

不在此快照：play_tts 的深層 happy path（FIFO + ffmpeg + threaded play + after callback）
需要真實音訊基礎設施、且正是 Plan 12 要取代的部分；此處只鎖定狀態機 guard、
queue counter 帳目、dual 順序、flush 契約、broadcast/radio 的 playback_lock 競爭、
與 tts_queue_duration 的外部讀取。drop / storm guard 已由 test_tts_storm_fallback.py 覆蓋。
"""
from __future__ import annotations

import asyncio
import os
import tempfile

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
    cog.radio_paused = False
    cog.is_playing_audio = False
    cog.tts_queue_duration = 0.0
    return cog


class _SpyLock:
    """包真 asyncio.Lock，記錄 __aenter__ 次數，用於斷言「有競爭 playback_lock」。"""

    def __init__(self):
        self.entered = 0
        self._real = asyncio.Lock()

    async def __aenter__(self):
        self.entered += 1
        return await self._real.__aenter__()

    async def __aexit__(self, *exc):
        return await self._real.__aexit__(*exc)


def _connected_vc(playing=False):
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = playing
    # play(source, after=...) 立即呼叫 after，模擬播放結束
    vc.play = MagicMock(side_effect=lambda source, after=None: after(None) if after else None)
    return vc


# ── play_tts 狀態機 guards ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_play_tts_game_mode_drops_silently():
    cog = _make_cog()
    cog.game_mode = True
    cog._tts_protected = False
    await cog.play_tts("哈囉")
    assert cog.tts_queue_duration == 0.0
    assert not cog.active_text_channel.send.called


@pytest.mark.asyncio
async def test_play_tts_empty_text_is_noop():
    cog = _make_cog()
    await cog.play_tts("")
    await cog.play_tts("   ")
    assert cog.tts_queue_duration == 0.0
    assert not cog.active_text_channel.send.called


@pytest.mark.asyncio
async def test_play_tts_no_voice_client_restores_queue_counter():
    """無連線 VoiceClient → 靜默退出、counter 加了又扣回淨零（不貼文）。"""
    cog = _make_cog()
    cog._tts_protected = True  # 繞過 silence gate / drop / storm，直達 no-vc 分支
    cog.bot.voice_clients = []
    await cog.play_tts("測試")
    assert cog.tts_queue_duration == 0.0
    assert not cog.active_text_channel.send.called


# ── tts_flush 契約 ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tts_flush_stops_and_clears_queue_and_resets_flag():
    cog = _make_cog()
    cog.tts_queue_duration = 5.0
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = True
    cog.bot.voice_clients = [vc]
    await cog.tts_flush()
    assert vc.stop.called
    assert cog.tts_queue_duration == 0.0
    assert cog._tts_flush_requested is False


# ── play_dual_dialogue 順序 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_play_dual_dialogue_plays_segments_in_order():
    cog = _make_cog()
    cog.play_tts = AsyncMock()
    segments = [
        {"voice": "marvin", "text": "我跑題了"},
        {"voice": "marmo", "text": "別聽馬文的"},
    ]
    with patch.dict(os.environ, {"MARMO_VOICE": "zh-TW-HsiaoYuNeural"}):
        await cog.play_dual_dialogue(segments)
    assert cog.play_tts.await_count == 2
    first = cog.play_tts.await_args_list[0]
    second = cog.play_tts.await_args_list[1]
    assert first.args[0] == "我跑題了"
    assert first.kwargs["voice"] is None
    assert first.kwargs["emotion_tag"] == "neutral"
    assert second.args[0] == "別聽馬文的"
    assert second.kwargs["voice"] == "zh-TW-HsiaoYuNeural"
    assert second.kwargs["emotion_tag"] == "marmo"
    assert cog._tts_interrupted is False


@pytest.mark.asyncio
async def test_play_dual_dialogue_empty_segments_noop():
    cog = _make_cog()
    cog.play_tts = AsyncMock()
    await cog.play_dual_dialogue([])
    assert cog.play_tts.await_count == 0


# ── broadcast / radio 競爭 playback_lock ─────────────────────────────────────

@pytest.mark.asyncio
async def test_play_local_file_broadcast_competes_playback_lock():
    cog = _make_cog()
    spy = _SpyLock()
    cog.playback_lock = spy
    vc = _connected_vc(playing=False)
    cog.bot.voice_clients = [vc]
    with tempfile.NamedTemporaryFile(suffix=".mp3") as f, \
         patch("discord.FFmpegPCMAudio", MagicMock()):
        await cog.play_local_file(f.name)
    assert spy.entered == 1
    assert vc.play.called
    assert cog.is_playing_audio is False


@pytest.mark.asyncio
async def test_play_radio_song_competes_playback_lock():
    cog = _make_cog()
    cog.radio_mode = True
    cog.radio_volume = 0.30
    spy = _SpyLock()
    cog.playback_lock = spy
    vc = _connected_vc(playing=False)
    cog.bot.voice_clients = [vc]
    with tempfile.NamedTemporaryFile(suffix=".mp3") as f, \
         patch("discord.FFmpegPCMAudio", MagicMock()), \
         patch("discord.PCMVolumeTransformer", MagicMock()):
        await cog.play_radio_song(f.name)
    assert spy.entered == 1
    assert vc.play.called


# ── tts_queue_duration 外部讀取（marvin_tts_clear owner gate）────────────────

@pytest.mark.asyncio
async def test_marvin_tts_clear_owner_reads_queue_then_flushes():
    from cogs.voice_controller import _NEMOCLAW_OWNER_ID
    cog = _make_cog()
    cog.tts_queue_duration = 5.0
    cog.tts_flush = AsyncMock()
    interaction = MagicMock()
    interaction.user.id = _NEMOCLAW_OWNER_ID
    interaction.response.send_message = AsyncMock()
    await cog.marvin_tts_clear.callback(cog, interaction)
    assert interaction.response.send_message.called
    assert "5.0" in interaction.response.send_message.call_args.args[0]
    cog.tts_flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_marvin_tts_clear_non_owner_denied_without_flush():
    from cogs.voice_controller import _NEMOCLAW_OWNER_ID
    cog = _make_cog()
    cog.tts_flush = AsyncMock()
    interaction = MagicMock()
    interaction.user.id = _NEMOCLAW_OWNER_ID + 1
    interaction.response.send_message = AsyncMock()
    await cog.marvin_tts_clear.callback(cog, interaction)
    assert interaction.response.send_message.called
    cog.tts_flush.assert_not_awaited()
