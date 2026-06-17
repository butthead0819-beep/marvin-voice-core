"""
TDD：語音控制 marvin_radio 播放行為

測試語音指令 stop / pause / resume / skip 對 radio_mode 的影響。
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
    bot.router = MagicMock()
    bot.router._call_llm = AsyncMock(return_value="ok")
    bot.router._background_intent_enrich = AsyncMock()
    bot.router.memory = MagicMock()
    bot.router.memory.get_player_data.return_value = {}
    bot.router.atmosphere_tracker = None
    bot.router.wake_fusion = None
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_harvest = MagicMock(return_value="test query")
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    bot.engine.post_summon_callback = None

    with patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        cog = VoiceController(bot)

    cog.active_text_channel = AsyncMock()
    _placeholder_msg = MagicMock()
    _placeholder_msg.edit = AsyncMock()
    _placeholder_msg.delete = AsyncMock()
    cog.active_text_channel.send = AsyncMock(return_value=_placeholder_msg)
    cog.stt_logger = MagicMock()
    cog.stream_queue = []
    cog.stream_history = []
    cog.stream_mode = False
    cog.radio_mode = False
    cog.radio_paused = False
    cog.stream_paused = False
    cog.is_playing_audio = False
    cog.tts_queue_duration = 0.0
    return cog


def _make_vc(playing: bool = True, paused: bool = False) -> MagicMock:
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = playing
    vc.is_paused.return_value = paused
    vc.pause = MagicMock()
    vc.resume = MagicMock()
    vc.stop_playing = MagicMock()
    return vc


# ── stop ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_voice_stop_radio_mode_calls_stop_radio():
    """stop 指令：只有 radio_mode 開啟時，應呼叫 stop_radio() 並貼成功訊息。"""
    cog = _make_cog()
    cog.radio_mode = True
    cog.stream_mode = False
    cog.stop_radio = AsyncMock()

    await cog._handle_voice_music_command("狗與露", "停止播放", "stop")

    cog.stop_radio.assert_awaited_once()
    cog.active_text_channel.send.assert_called_once()
    sent = cog.active_text_channel.send.call_args[0][0]
    assert "⏹️" in sent


@pytest.mark.asyncio
async def test_voice_stop_no_modes_active_sends_error():
    """stop 指令：radio_mode 和 stream_mode 均關閉時，回傳錯誤提示。"""
    cog = _make_cog()
    cog.radio_mode = False
    cog.stream_mode = False
    cog.stop_radio = AsyncMock()
    cog.stop_stream = AsyncMock()

    await cog._handle_voice_music_command("狗與露", "停止播放", "stop")

    cog.stop_radio.assert_not_awaited()
    cog.stop_stream.assert_not_awaited()
    cog.active_text_channel.send.assert_called_once()
    sent = cog.active_text_channel.send.call_args[0][0]
    assert "😑" in sent


@pytest.mark.asyncio
async def test_voice_stop_both_modes_active_stops_both():
    """stop 指令：radio_mode 和 stream_mode 同時開啟時，兩者都要停止。"""
    cog = _make_cog()
    cog.radio_mode = True
    cog.stream_mode = True
    cog.stop_radio = AsyncMock()
    cog.stop_stream = AsyncMock()

    await cog._handle_voice_music_command("狗與露", "停止播放", "stop")

    cog.stop_radio.assert_awaited_once()
    cog.stop_stream.assert_awaited_once()
    cog.active_text_channel.send.assert_called_once()


# ── pause ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_voice_pause_radio_mode_pauses_vc():
    """pause 指令：只有 radio_mode 開啟時，應 pause vc 並設定 radio_paused=True。"""
    cog = _make_cog()
    cog._mixer = MagicMock()
    vc = _make_vc()
    cog.bot.voice_clients = [vc]
    cog.radio_mode = True
    cog.radio_paused = False
    cog.stream_mode = False

    await cog._handle_voice_music_command("狗與露", "暫停播放", "pause")

    cog._mixer.set_paused.assert_called_once_with(True)
    assert cog.radio_paused is True
    cog.active_text_channel.send.assert_called_once()
    sent = cog.active_text_channel.send.call_args[0][0]
    assert "⏸️" in sent


@pytest.mark.asyncio
async def test_voice_pause_radio_already_paused_no_double_pause():
    """pause 指令：radio_paused=True 時不應重複呼叫 vc.pause()。"""
    cog = _make_cog()
    vc = _make_vc(playing=False, paused=True)
    cog.bot.voice_clients = [vc]
    cog.radio_mode = True
    cog.radio_paused = True
    cog.stream_mode = False

    await cog._handle_voice_music_command("狗與露", "暫停播放", "pause")

    vc.pause.assert_not_called()


@pytest.mark.asyncio
async def test_voice_pause_no_modes_active_sends_error():
    """pause 指令：radio_mode 和 stream_mode 均關閉時，回傳錯誤提示。"""
    cog = _make_cog()
    vc = _make_vc()
    cog.bot.voice_clients = [vc]
    cog.radio_mode = False
    cog.stream_mode = False

    await cog._handle_voice_music_command("狗與露", "暫停播放", "pause")

    vc.pause.assert_not_called()
    cog.active_text_channel.send.assert_called_once()
    sent = cog.active_text_channel.send.call_args[0][0]
    assert "😑" in sent


# ── resume ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_voice_resume_radio_paused_resumes_vc():
    """resume 指令：radio_paused=True 時應 resume vc 並清除 radio_paused。"""
    cog = _make_cog()
    cog._mixer = MagicMock()
    vc = _make_vc(playing=False, paused=True)
    cog.bot.voice_clients = [vc]
    cog.radio_mode = True
    cog.radio_paused = True
    cog.stream_mode = False
    cog.stream_paused = False

    await cog._handle_voice_music_command("狗與露", "繼續播", "resume")

    cog._mixer.set_paused.assert_called_once_with(False)
    assert cog.radio_paused is False
    cog.active_text_channel.send.assert_called_once()
    sent = cog.active_text_channel.send.call_args[0][0]
    assert "▶️" in sent


@pytest.mark.asyncio
async def test_voice_resume_no_modes_paused_sends_error():
    """resume 指令：radio_paused 和 stream_paused 均為 False 時，回傳錯誤提示。"""
    cog = _make_cog()
    vc = _make_vc()
    cog.bot.voice_clients = [vc]
    cog.radio_mode = True
    cog.radio_paused = False
    cog.stream_mode = False
    cog.stream_paused = False

    await cog._handle_voice_music_command("狗與露", "繼續播", "resume")

    vc.resume.assert_not_called()
    cog.active_text_channel.send.assert_called_once()
    sent = cog.active_text_channel.send.call_args[0][0]
    assert "😑" in sent


# ── skip ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_voice_skip_radio_mode_stops_vc():
    """skip 指令：radio_mode 開啟時應 stop_playing()，觸發電台迴圈換下一首。"""
    cog = _make_cog()
    cog._mixer = MagicMock()
    vc = _make_vc()
    cog.bot.voice_clients = [vc]
    cog.radio_mode = True
    cog.stream_mode = False

    await cog._handle_voice_music_command("狗與露", "下一首", "skip")

    cog._mixer.clear_music.assert_called_once()
    cog.active_text_channel.send.assert_called_once()
    sent = cog.active_text_channel.send.call_args[0][0]
    assert "⏭️" in sent


@pytest.mark.asyncio
async def test_voice_skip_no_modes_active_sends_error():
    """skip 指令：radio_mode 和 stream_mode 均關閉時，回傳錯誤提示。"""
    cog = _make_cog()
    vc = _make_vc()
    cog.bot.voice_clients = [vc]
    cog.radio_mode = False
    cog.stream_mode = False

    await cog._handle_voice_music_command("狗與露", "下一首", "skip")

    vc.stop_playing.assert_not_called()
    cog.active_text_channel.send.assert_called_once()
    sent = cog.active_text_channel.send.call_args[0][0]
    assert "😑" in sent
