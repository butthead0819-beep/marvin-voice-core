"""Phase 1 TDD：stream_mode / radio_mode proxy property。

VC 的 stream_mode 和 radio_mode 透過 proxy 代理到 MusicCog：
  - MusicCog 存在：getter/setter 都通向 MusicCog
  - MusicCog 不存在：fallback 用 _stream_mode_local / _radio_mode_local
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import make_music_cog_mock


def _make_vc(music_cog=None):
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.tts_engine = MagicMock()
    bot.router = MagicMock()
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.post_summon_callback = None
    bot.cogs.get.side_effect = lambda name: {'MusicCog': music_cog}.get(name)
    with patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        vc = VoiceController(bot)
    vc.stt_logger = MagicMock()
    return vc


# --- stream_mode ---

def test_stream_mode_getter_returns_false_when_no_music_cog():
    vc = _make_vc(music_cog=None)
    assert vc.stream_mode is False


def test_stream_mode_getter_delegates_to_music_cog():
    mc = make_music_cog_mock(stream_mode=True)
    vc = _make_vc(music_cog=mc)
    assert vc.stream_mode is True


def test_stream_mode_setter_writes_to_music_cog():
    mc = make_music_cog_mock(stream_mode=False)
    vc = _make_vc(music_cog=mc)
    vc.stream_mode = True
    assert mc.stream_mode is True


def test_stream_mode_setter_uses_local_fallback_when_no_music_cog():
    vc = _make_vc(music_cog=None)
    vc.stream_mode = True
    assert vc._stream_mode_local is True
    assert vc.stream_mode is True  # getter also reads local fallback


# --- radio_mode ---

def test_radio_mode_getter_returns_false_when_no_music_cog():
    vc = _make_vc(music_cog=None)
    assert vc.radio_mode is False


def test_radio_mode_getter_delegates_to_music_cog():
    mc = make_music_cog_mock(radio_mode=True)
    vc = _make_vc(music_cog=mc)
    assert vc.radio_mode is True


def test_radio_mode_setter_writes_to_music_cog():
    mc = make_music_cog_mock(radio_mode=False)
    vc = _make_vc(music_cog=mc)
    vc.radio_mode = True
    assert mc.radio_mode is True


def test_radio_mode_setter_uses_local_fallback_when_no_music_cog():
    vc = _make_vc(music_cog=None)
    vc.radio_mode = True
    assert vc._radio_mode_local is True
    assert vc.radio_mode is True


# --- proxy 不影響既有測試的 False 初始值 ---

def test_stream_mode_defaults_false():
    vc = _make_vc(music_cog=None)
    assert vc.stream_mode is False
    assert vc.radio_mode is False
