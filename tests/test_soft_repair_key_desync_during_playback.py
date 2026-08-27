"""TDD — key_desync_storm 的軟修復不得被「播放中」擋掉。

2026-08-27 incident：DJ autopilot 幾乎一直在放音樂 → soft_repair_connection()
開頭 `if self.is_playing_audio: return` 讓 secret_key desync 風暴的自癒每次都被跳過。
配合 sentinel_monitor_loop 的「120s 穩定重設 soft_repair_count」，變成
fire→skip→重設→fire 無限循環，CryptoError 解密風暴永不收斂 → voice_recv reader
thread 吃 GIL → AudioPlayer 送幀延遲 → 聽者端爆音（跟有沒有人講話無關）。

修法：key_desync_storm 這種「正在播的音訊本身已因風暴而爆音」的失效，
soft_repair_connection 必須無視 is_playing_audio 照樣重連。其他失效類型維持原樣
（播放中跳過，避免無謂中斷）。
"""
from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_cog():
    from cogs.voice_controller import VoiceController

    cog = VoiceController.__new__(VoiceController)
    cog.bot = MagicMock()
    cog.is_playing_audio = True  # ← DJ autopilot 常態
    cog.active_text_channel = None
    cog.soft_repair_count = 0
    cog.connection_time = 0.0
    cog.last_recovery_time = 0.0
    cog.dave_error_count = 0
    cog._plan12 = False
    cog.is_recovering = False
    cog.self_restart = AsyncMock()
    return cog


def _wire_reconnect_success(cog):
    old_vc = MagicMock()
    old_vc.channel = MagicMock()
    old_vc.channel.name = "語音"
    old_vc.disconnect = AsyncMock()

    new_vc = MagicMock()
    new_vc.is_connected.return_value = True
    new_vc.listen = MagicMock()
    new_vc.play = MagicMock()
    old_vc.channel.connect = AsyncMock(return_value=new_vc)

    cog.bot.voice_clients = [old_vc]
    cog.bot.engine.process_audio_slice = MagicMock()
    cog.bot.engine._handle_raw_speech_start = MagicMock()
    cog.bot.engine.conv_buffer.get_conversation_temperature = MagicMock()
    cog.report_sink_error = MagicMock()
    cog.stream_mode = False
    cog.radio_mode = False
    cog._on_key_desync_storm = MagicMock()
    return old_vc, new_vc


def _fake_engine_module():
    mod = types.ModuleType("discord_voice_engine")
    mod.RealtimeVADSink = MagicMock()
    mod.patch_voice_recv_key_sync = MagicMock()
    return mod


@pytest.mark.asyncio
async def test_key_desync_storm_repairs_even_while_playing():
    cog = _make_cog()
    old_vc, _new_vc = _wire_reconnect_success(cog)

    with patch.dict(sys.modules, {"discord_voice_engine": _fake_engine_module()}), \
         patch("asyncio.sleep", new=AsyncMock()):
        await cog.orchestrate_recovery("key_desync_storm")

    old_vc.disconnect.assert_awaited()  # 沒被 is_playing_audio 擋掉
    old_vc.channel.connect.assert_awaited()


@pytest.mark.asyncio
async def test_other_failure_still_skips_while_playing():
    """非 key_desync_storm 的失效，播放中仍跳過（維持原行為）。"""
    cog = _make_cog()
    old_vc, _new_vc = _wire_reconnect_success(cog)

    with patch.dict(sys.modules, {"discord_voice_engine": _fake_engine_module()}), \
         patch("asyncio.sleep", new=AsyncMock()):
        await cog.orchestrate_recovery("dave_decrypt_failure")

    old_vc.disconnect.assert_not_awaited()
    old_vc.channel.connect.assert_not_awaited()
