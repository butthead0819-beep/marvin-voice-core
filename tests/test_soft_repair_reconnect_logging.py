"""TDD — soft_repair_connection 失敗訊息必須帶例外身分（2026-06-16 incident）。

CryptoError 風暴觸發 Sentinel 軟修復；軟修復的 channel.connect(timeout=60.0)
逾時拋 asyncio.TimeoutError。TimeoutError 的 str() 是空字串，舊版 log 寫
f"...: {e}" → 出來是 "軟修復重連崩潰: "（冒號後全空），incident 完全無法判斷
重連到底是逾時、被取消、還是 ClientException。

修法：失敗路徑改用 repr(e)（或型別名），讓任何空訊息例外都至少留下型別。
"""
from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_cog():
    """建空殼 VoiceController，只塞 soft_repair_connection 會碰到的 attribute。"""
    from cogs.voice_controller import VoiceController

    cog = VoiceController.__new__(VoiceController)
    cog.bot = MagicMock()
    cog.is_playing_audio = False
    cog.active_text_channel = None
    cog.soft_repair_count = 1
    cog.connection_time = 0.0
    cog.self_restart = AsyncMock()
    return cog


def _wire_reconnect_failure(cog, connect_exc):
    """讓 disconnect 成功、reconnect（channel.connect）拋 connect_exc。"""
    vc = MagicMock()
    vc.channel = MagicMock()
    vc.channel.name = "語音"
    vc.disconnect = AsyncMock()
    vc.channel.connect = AsyncMock(side_effect=connect_exc)
    cog.bot.voice_clients = [vc]


def _fake_engine_module():
    """假的 discord_voice_engine，避免重連路徑 import 真模組的重副作用。"""
    mod = types.ModuleType("discord_voice_engine")
    mod.RealtimeVADSink = MagicMock()
    mod.patch_voice_recv_key_sync = MagicMock()
    return mod


@pytest.mark.asyncio
async def test_reconnect_timeout_restart_reason_includes_exc_type():
    """空訊息例外（TimeoutError）→ 傳給 self_restart 的 reason 必須含型別名，不得空白。"""
    cog = _make_cog()
    _wire_reconnect_failure(cog, asyncio.TimeoutError())

    with patch.dict(sys.modules, {"discord_voice_engine": _fake_engine_module()}), \
         patch("asyncio.sleep", new=AsyncMock()):
        await cog.soft_repair_connection(reason="底層失效 (CryptoError)")

    cog.self_restart.assert_awaited_once()
    reason = cog.self_restart.await_args.kwargs["reason"]
    # 修復前：reason == "軟修復重連崩潰: " → 不含 TimeoutError → 紅
    assert "TimeoutError" in reason, f"reason 應帶例外型別，實際: {reason!r}"


@pytest.mark.asyncio
async def test_reconnect_exception_with_message_preserved():
    """有訊息的例外 → 訊息仍要保留（不能因改格式而吃掉內容）。"""
    cog = _make_cog()
    _wire_reconnect_failure(cog, RuntimeError("voice ws closed 4006"))

    with patch.dict(sys.modules, {"discord_voice_engine": _fake_engine_module()}), \
         patch("asyncio.sleep", new=AsyncMock()):
        await cog.soft_repair_connection(reason="底層失效 (CryptoError)")

    cog.self_restart.assert_awaited_once()
    reason = cog.self_restart.await_args.kwargs["reason"]
    assert "voice ws closed 4006" in reason, f"reason 應保留訊息，實際: {reason!r}"
