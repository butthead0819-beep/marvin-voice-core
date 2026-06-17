"""Phase 3 TDD：radio subsystem state proxy properties。

6 個 radio 狀態屬性透過 proxy 代理到 MusicCog：
  - MusicCog 存在：所有讀寫通向 MusicCog
  - MusicCog 不存在：fallback 用 _X_local 本地欄位
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import make_music_cog_mock


def _make_mc_radio(**kwargs):
    mc = make_music_cog_mock()
    mc.radio_task = kwargs.get("radio_task", None)
    mc.radio_volume = kwargs.get("radio_volume", 0.10)
    mc._radio_song_list = kwargs.get("_radio_song_list", [])
    mc._radio_source = kwargs.get("_radio_source", None)
    mc._radio_fade_task = kwargs.get("_radio_fade_task", None)
    mc.radio_paused = kwargs.get("radio_paused", False)
    return mc


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


# ── radio_task ────────────────────────────────────────────────────────────────

def test_radio_task_none_when_no_music_cog():
    vc = _make_vc(music_cog=None)
    assert vc.radio_task is None


def test_radio_task_getter_delegates_to_music_cog():
    task = MagicMock()
    mc = _make_mc_radio(radio_task=task)
    vc = _make_vc(music_cog=mc)
    assert vc.radio_task is task


def test_radio_task_setter_writes_to_music_cog():
    mc = _make_mc_radio(radio_task=None)
    vc = _make_vc(music_cog=mc)
    task = MagicMock()
    vc.radio_task = task
    assert mc.radio_task is task


def test_radio_task_setter_uses_local_fallback():
    vc = _make_vc(music_cog=None)
    task = MagicMock()
    vc.radio_task = task
    assert vc._radio_task_local is task
    assert vc.radio_task is task


# ── radio_volume ──────────────────────────────────────────────────────────────

def test_radio_volume_defaults_when_no_music_cog():
    vc = _make_vc(music_cog=None)
    assert vc.radio_volume == pytest.approx(0.10)


def test_radio_volume_getter_delegates_to_music_cog():
    mc = _make_mc_radio(radio_volume=0.40)
    vc = _make_vc(music_cog=mc)
    assert vc.radio_volume == pytest.approx(0.40)


def test_radio_volume_setter_writes_to_music_cog():
    mc = _make_mc_radio(radio_volume=0.10)
    vc = _make_vc(music_cog=mc)
    vc.radio_volume = 0.20
    assert mc.radio_volume == pytest.approx(0.20)


def test_radio_volume_setter_uses_local_fallback():
    vc = _make_vc(music_cog=None)
    vc.radio_volume = 0.15
    assert vc._radio_volume_local == pytest.approx(0.15)
    assert vc.radio_volume == pytest.approx(0.15)


# ── radio_paused ──────────────────────────────────────────────────────────────

def test_radio_paused_false_when_no_music_cog():
    vc = _make_vc(music_cog=None)
    assert vc.radio_paused is False


def test_radio_paused_getter_delegates_to_music_cog():
    mc = _make_mc_radio(radio_paused=True)
    vc = _make_vc(music_cog=mc)
    assert vc.radio_paused is True


def test_radio_paused_setter_writes_to_music_cog():
    mc = _make_mc_radio(radio_paused=False)
    vc = _make_vc(music_cog=mc)
    vc.radio_paused = True
    assert mc.radio_paused is True


def test_radio_paused_setter_uses_local_fallback():
    vc = _make_vc(music_cog=None)
    vc.radio_paused = True
    assert vc._radio_paused_local is True
    assert vc.radio_paused is True


# ── _radio_song_list ──────────────────────────────────────────────────────────

def test_radio_song_list_empty_when_no_music_cog():
    vc = _make_vc(music_cog=None)
    assert vc._radio_song_list == []


def test_radio_song_list_in_place_mutation_goes_to_music_cog():
    mc = _make_mc_radio(_radio_song_list=[])
    vc = _make_vc(music_cog=mc)
    vc._radio_song_list.append("song_url")
    assert mc._radio_song_list == ["song_url"]


# ── _radio_source / _radio_fade_task ─────────────────────────────────────────

def test_radio_source_none_when_no_music_cog():
    vc = _make_vc(music_cog=None)
    assert vc._radio_source is None


def test_radio_source_setter_writes_to_music_cog():
    mc = _make_mc_radio()
    vc = _make_vc(music_cog=mc)
    src = MagicMock()
    vc._radio_source = src
    assert mc._radio_source is src


def test_radio_fade_task_none_when_no_music_cog():
    vc = _make_vc(music_cog=None)
    assert vc._radio_fade_task is None


def test_radio_fade_task_setter_writes_to_music_cog():
    mc = _make_mc_radio()
    vc = _make_vc(music_cog=mc)
    fade = MagicMock()
    vc._radio_fade_task = fade
    assert mc._radio_fade_task is fade
