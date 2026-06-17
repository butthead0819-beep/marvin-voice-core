"""Phase 2 TDD：stream subsystem state proxy properties。

13 個 stream 狀態屬性透過 proxy 代理到 MusicCog：
  - MusicCog 存在：所有讀寫通向 MusicCog
  - MusicCog 不存在：fallback 用 _X_local 本地欄位
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import make_music_cog_mock


def _make_mc_full(**kwargs):
    """建包含所有 Phase 2 stream state 的 MusicCog mock。"""
    mc = make_music_cog_mock()
    # Phase 2 state defaults
    mc.stream_volume = kwargs.get("stream_volume", 0.10)
    mc._stream_play_gen = kwargs.get("_stream_play_gen", 0)
    mc._current_stream_url = kwargs.get("_current_stream_url", None)
    mc._stream_norm_gain = kwargs.get("_stream_norm_gain", {})
    mc._last_user_song_seed = kwargs.get("_last_user_song_seed", None)
    mc.stream_queue = kwargs.get("stream_queue", [])
    mc.stream_task = kwargs.get("stream_task", None)
    mc._current_stream_info = kwargs.get("_current_stream_info", None)
    mc.stream_history = kwargs.get("stream_history", [])
    mc.stream_paused = kwargs.get("stream_paused", False)
    mc._current_lyrics = kwargs.get("_current_lyrics", None)
    mc._current_stream_comment = kwargs.get("_current_stream_comment", None)
    mc._active_control_view = kwargs.get("_active_control_view", None)
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


# ── stream_volume ─────────────────────────────────────────────────────────────

def test_stream_volume_getter_defaults_when_no_music_cog():
    vc = _make_vc(music_cog=None)
    assert vc.stream_volume == pytest.approx(0.10)


def test_stream_volume_getter_delegates_to_music_cog():
    mc = _make_mc_full(stream_volume=0.50)
    vc = _make_vc(music_cog=mc)
    assert vc.stream_volume == pytest.approx(0.50)


def test_stream_volume_setter_writes_to_music_cog():
    mc = _make_mc_full(stream_volume=0.10)
    vc = _make_vc(music_cog=mc)
    vc.stream_volume = 0.30
    assert mc.stream_volume == pytest.approx(0.30)


def test_stream_volume_setter_uses_local_fallback():
    vc = _make_vc(music_cog=None)
    vc.stream_volume = 0.25
    assert vc._stream_volume_local == pytest.approx(0.25)
    assert vc.stream_volume == pytest.approx(0.25)


# ── stream_queue ──────────────────────────────────────────────────────────────

def test_stream_queue_getter_empty_when_no_music_cog():
    vc = _make_vc(music_cog=None)
    assert vc.stream_queue == []


def test_stream_queue_getter_delegates_to_music_cog():
    song = {"title": "Test Song", "url": "https://example.com/audio"}
    mc = _make_mc_full(stream_queue=[song])
    vc = _make_vc(music_cog=mc)
    assert vc.stream_queue == [song]


def test_stream_queue_in_place_mutation_goes_to_music_cog():
    mc = _make_mc_full(stream_queue=[])
    vc = _make_vc(music_cog=mc)
    vc.stream_queue.append({"title": "Song A"})
    assert mc.stream_queue == [{"title": "Song A"}]


def test_stream_queue_setter_writes_to_music_cog():
    mc = _make_mc_full(stream_queue=[{"title": "old"}])
    vc = _make_vc(music_cog=mc)
    vc.stream_queue = []
    assert mc.stream_queue == []


def test_stream_queue_setter_uses_local_fallback():
    vc = _make_vc(music_cog=None)
    vc.stream_queue = [{"title": "Local"}]
    assert vc._stream_queue_local == [{"title": "Local"}]
    assert vc.stream_queue == [{"title": "Local"}]


# ── _current_stream_info ──────────────────────────────────────────────────────

def test_current_stream_info_none_when_no_music_cog():
    vc = _make_vc(music_cog=None)
    assert vc._current_stream_info is None


def test_current_stream_info_getter_delegates_to_music_cog():
    info = {"title": "Now Playing", "url": "https://example.com"}
    mc = _make_mc_full(_current_stream_info=info)
    vc = _make_vc(music_cog=mc)
    assert vc._current_stream_info == info


def test_current_stream_info_setter_writes_to_music_cog():
    mc = _make_mc_full(_current_stream_info=None)
    vc = _make_vc(music_cog=mc)
    info = {"title": "New Song"}
    vc._current_stream_info = info
    assert mc._current_stream_info == info


def test_current_stream_info_setter_uses_local_fallback():
    vc = _make_vc(music_cog=None)
    info = {"title": "Fallback Song"}
    vc._current_stream_info = info
    assert vc._current_stream_info_local == info
    assert vc._current_stream_info == info


# ── stream_paused ─────────────────────────────────────────────────────────────

def test_stream_paused_false_when_no_music_cog():
    vc = _make_vc(music_cog=None)
    assert vc.stream_paused is False


def test_stream_paused_getter_delegates_to_music_cog():
    mc = _make_mc_full(stream_paused=True)
    vc = _make_vc(music_cog=mc)
    assert vc.stream_paused is True


def test_stream_paused_setter_writes_to_music_cog():
    mc = _make_mc_full(stream_paused=False)
    vc = _make_vc(music_cog=mc)
    vc.stream_paused = True
    assert mc.stream_paused is True


def test_stream_paused_setter_uses_local_fallback():
    vc = _make_vc(music_cog=None)
    vc.stream_paused = True
    assert vc._stream_paused_local is True
    assert vc.stream_paused is True


# ── stream_history ────────────────────────────────────────────────────────────

def test_stream_history_empty_when_no_music_cog():
    vc = _make_vc(music_cog=None)
    assert vc.stream_history == []


def test_stream_history_in_place_append_goes_to_music_cog():
    mc = _make_mc_full(stream_history=[])
    vc = _make_vc(music_cog=mc)
    vc.stream_history.append({"title": "Past Song"})
    assert mc.stream_history == [{"title": "Past Song"}]


# ── _current_lyrics ───────────────────────────────────────────────────────────

def test_current_lyrics_none_when_no_music_cog():
    vc = _make_vc(music_cog=None)
    assert vc._current_lyrics is None


def test_current_lyrics_delegates_to_music_cog():
    mc = _make_mc_full(_current_lyrics="La la la")
    vc = _make_vc(music_cog=mc)
    assert vc._current_lyrics == "La la la"


# ── _active_control_view ──────────────────────────────────────────────────────

def test_active_control_view_none_when_no_music_cog():
    vc = _make_vc(music_cog=None)
    assert vc._active_control_view is None


def test_active_control_view_setter_writes_to_music_cog():
    mc = _make_mc_full()
    vc = _make_vc(music_cog=mc)
    view = MagicMock()
    vc._active_control_view = view
    assert mc._active_control_view is view
