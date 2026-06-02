"""T2 — cog_unload 釋放 active view refs，防 hot reload 殘留雙 cog 實例。

View 持 controller 強引用；cog hot reload 時若 view 還活著，舊 cog 無法回收。
cog_unload 必須 stop 所有 active view 讓 Discord 釋出，斷開 view→cog 強引用。
"""
from __future__ import annotations

import weakref

import pytest
from unittest.mock import MagicMock, patch


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
    return cog


def test_active_views_initialized_as_weakset():
    cog = _make_cog()
    assert isinstance(cog._active_views, weakref.WeakSet)


def test_release_active_views_stops_each_active_view():
    cog = _make_cog()
    v1, v2 = MagicMock(), MagicMock()
    cog._active_views.add(v1)
    cog._active_views.add(v2)
    cog._release_active_views()
    v1.stop.assert_called_once()
    v2.stop.assert_called_once()
