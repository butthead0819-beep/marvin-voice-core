"""Plan 12 god-class 接線（T3 sub-2a）— flag + mixer 實例化 + ensure-playing。

flag=off：mixer None、_ensure_mixer_playing no-op（舊路徑零改變）。
flag=on：cog 持 LocalMixingAudioSource、ensure 在 idle vc 上 play 一個 MixerPlaybackAdapter。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from local_mixing_source import LocalMixingAudioSource, MixerPlaybackAdapter


def _make_cog(plan12: bool, monkeypatch):
    if plan12:
        monkeypatch.setenv("PLAN12_LOCAL_MIX", "true")
    else:
        monkeypatch.delenv("PLAN12_LOCAL_MIX", raising=False)
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.get_estimated_duration.return_value = 2.0
    with patch("discord_voice_engine.faster_whisper", None, create=True):
        from discord_voice_engine import DiscordVoiceEngine
        bot.engine = DiscordVoiceEngine(bot)
    with patch("discord.ext.tasks.loop", lambda *a, **kw: lambda f: f), \
         patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        return VoiceController(bot)


def _idle_vc():
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = False
    return vc


def test_flag_off_no_mixer_and_ensure_noops(monkeypatch):
    cog = _make_cog(plan12=False, monkeypatch=monkeypatch)
    assert cog._plan12 is False
    assert cog._mixer is None
    vc = _idle_vc()
    assert cog._ensure_mixer_playing(vc) is False
    assert not vc.play.called  # 舊路徑零干擾


def test_flag_on_builds_mixer_and_ensure_plays(monkeypatch):
    cog = _make_cog(plan12=True, monkeypatch=monkeypatch)
    assert cog._plan12 is True
    assert isinstance(cog._mixer, LocalMixingAudioSource)
    vc = _idle_vc()
    assert cog._ensure_mixer_playing(vc) is True
    assert vc.play.call_count == 1
    assert isinstance(vc.play.call_args.args[0], MixerPlaybackAdapter)


def test_flag_on_ensure_idempotent_when_playing(monkeypatch):
    cog = _make_cog(plan12=True, monkeypatch=monkeypatch)
    vc = _idle_vc()
    vc.is_playing.return_value = True
    assert cog._ensure_mixer_playing(vc) is False
    assert not vc.play.called
