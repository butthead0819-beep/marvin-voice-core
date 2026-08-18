"""TDD — soft_repair_connection 重連成功後要補 active_text_channel fallback。

2026-08-17 的修法（0bde4a4）只補了 auto_rejoin_on_boot() 一條路：process 重啟/
開機時 active_text_channel 是 None → 回台後用語音頻道自帶文字區頂上，卡片/控制台
才不會永久沉默。但 Sentinel 的另一條自動重連路徑 soft_repair_connection()（vc
殭屍 is_connected()==False 時的靜默重連）完全沒補——若 active_text_channel 在
軟修復發生當下已是 None（例如殭屍斷線同時卡在 dismiss 的 disconnect() 判斷式
里被跳過、active_text_channel 卻已被清空），軟修復重連成功後 active_text_channel
仍是 None，之後所有音樂卡片/控制台永久 [Card] 跳過貼卡，直到有人手動 /summon。

修法：soft_repair_connection() 重連成功後鏡像 auto_rejoin_on_boot() 的 fallback：
active_text_channel 是 None 就用回台的語音頻道自帶文字區頂上。
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
    cog.is_playing_audio = False
    cog.active_text_channel = None
    cog.soft_repair_count = 1
    cog.connection_time = 0.0
    cog.last_recovery_time = 0.0
    cog.dave_error_count = 0
    cog._plan12 = False
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
async def test_soft_repair_success_restores_missing_active_text_channel():
    """active_text_channel 在軟修復當下是 None → 重連成功後要用回台頻道頂上。"""
    cog = _make_cog()
    old_vc, _new_vc = _wire_reconnect_success(cog)

    with patch.dict(sys.modules, {"discord_voice_engine": _fake_engine_module()}), \
         patch("asyncio.sleep", new=AsyncMock()):
        await cog.soft_repair_connection(reason="測試")

    assert cog.active_text_channel is old_vc.channel, (
        "soft_repair_connection 重連成功後，active_text_channel 仍是 None，"
        "之後音樂卡片/控制台會永久 [Card] 跳過貼卡"
    )


@pytest.mark.asyncio
async def test_soft_repair_success_keeps_existing_active_text_channel():
    """active_text_channel 若已存在（例如先前 /summon 設過）→ 軟修復不得覆蓋。"""
    cog = _make_cog()
    old_vc, _new_vc = _wire_reconnect_success(cog)
    existing_channel = MagicMock(name="既有文字頻道")
    cog.active_text_channel = existing_channel

    with patch.dict(sys.modules, {"discord_voice_engine": _fake_engine_module()}), \
         patch("asyncio.sleep", new=AsyncMock()):
        await cog.soft_repair_connection(reason="測試")

    assert cog.active_text_channel is existing_channel
