"""
tests/test_car_mode_stream_arm.py
TDD：MARVIN_CAR_MODE=1 時 setup_browser_satellite 必須在啟動當下主動把 mixer 泵開起來。

背景（2026-07-23 ESP32 puck 實測）：mixer 泵預設 on-demand，只有真的有 TTS/音樂要推
才會 arm。車載模式雖然把 LocalSpeakerDevice 設成 persistent=True，但如果開機後還沒
有任何內容觸發（例如 on_arrive 選歌失敗），泵從未被 arm 過，/audio_stream 訂閱者收
不到任何 frame。ESP32 firmware 用 Arduino Stream 預設 1 秒讀取逾時，空等 1 秒就判斷
斷線、重連，形成永久 1 秒重連迴圈。修法＝車載分支啟動時立刻 arm 泵（推 silence 幀），
不等第一句話。
"""
import os
from unittest.mock import MagicMock

import pytest


def _make_mock_bot():
    mock_vc = MagicMock()
    bot = MagicMock()
    bot.load_extension = MagicMock()

    async def _noop(*_a, **_kw):
        return None
    bot.load_extension.side_effect = _noop
    bot.engine = MagicMock()
    bot.engine.start = MagicMock()
    bot.cogs = MagicMock()
    bot.cogs.get = MagicMock(return_value=mock_vc)
    bot.loop = MagicMock()
    return bot, mock_vc


@pytest.mark.asyncio
async def test_car_mode_arms_mixer_pump_immediately_on_setup(monkeypatch):
    monkeypatch.setenv("MARVIN_CAR_MODE", "1")
    from main_satellite import setup_browser_satellite
    bot, mock_vc = _make_mock_bot()
    await setup_browser_satellite(bot)
    mock_vc._ensure_mixer_playing.assert_called_once()


@pytest.mark.asyncio
async def test_non_car_mode_does_not_require_immediate_arm(monkeypatch):
    monkeypatch.delenv("MARVIN_CAR_MODE", raising=False)
    from main_satellite import setup_browser_satellite
    bot, mock_vc = _make_mock_bot()
    await setup_browser_satellite(bot)
    mock_vc._ensure_mixer_playing.assert_not_called()
