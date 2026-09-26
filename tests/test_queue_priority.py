"""TDD: queue_priority.user_song_insert_index（自 music_cog 搬出，行為不變 → 補真人點歌優先）。"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

from queue_priority import remaining_seconds, user_song_insert_index


def _u(name, vid):
    return {"title": vid, "requested_by": name,
            "webpage_url": f"https://youtu.be/{vid}", "url": "x"}


def _m(vid):
    return {"title": vid, "requested_by": "Marvin推薦（為showay）",
            "webpage_url": f"https://youtu.be/{vid}", "url": "x"}


# ── 非 stream_mode：舊行為 ────────────────────────────────────────────────

def test_non_stream_empty_queue():
    assert user_song_insert_index([], False) == 0


def test_non_stream_all_user_songs_goes_to_end():
    assert user_song_insert_index([_u("a", "1"), _u("b", "2")], False) == 2


def test_non_stream_all_marvin_goes_to_front():
    assert user_song_insert_index([_m("1"), _m("2")], False) == 0


def test_non_stream_user_marvin_boundary():
    assert user_song_insert_index([_u("a", "1"), _m("2"), _u("b", "3")], False) == 1


# ── stream_mode：slot 0 神聖（2026-08-27 爆音修） ─────────────────────────

def test_stream_empty_queue():
    assert user_song_insert_index([], True) == 0


def test_stream_all_marvin():
    assert user_song_insert_index([_m("1"), _m("2")], True) == 1


def test_stream_marvin_user_user_marvin():
    assert user_song_insert_index([_m("1"), _u("a", "2"), _u("b", "3"), _m("4")], True) == 3


def test_stream_single_user_song():
    assert user_song_insert_index([_u("a", "1")], True) == 1


# ── remaining_seconds ─────────────────────────────────────────────────────

def test_remaining_seconds_basic():
    assert remaining_seconds({"duration": 200}, 1000, now=1050) == 150


def test_remaining_seconds_with_highlight_start():
    assert remaining_seconds({"duration": 200, "highlight_start_s": 30}, 1000, now=1050) == 120


def test_remaining_seconds_none_when_no_current_info():
    assert remaining_seconds(None, 1000, now=1050) is None


def test_remaining_seconds_none_when_no_start_time():
    assert remaining_seconds({"duration": 200}, None, now=1050) is None


def test_remaining_seconds_none_when_duration_missing():
    assert remaining_seconds({}, 1000, now=1050) is None


def test_remaining_seconds_none_when_duration_zero():
    assert remaining_seconds({"duration": 0}, 1000, now=1050) is None


# ── 真人點歌優先：剩餘時間夠 → 插最前 ────────────────────────────────────────

def test_priority_inserts_front_when_remaining_enough():
    assert user_song_insert_index([_m("1"), _m("2")], True, remaining_s=100) == 0


def test_priority_falls_back_when_remaining_not_enough():
    assert user_song_insert_index([_m("1"), _m("2")], True, remaining_s=44) == 1


def test_priority_falls_back_when_remaining_unknown():
    assert user_song_insert_index([_m("1"), _m("2")], True, remaining_s=None) == 1


def test_priority_boundary_exactly_45s():
    assert user_song_insert_index([_m("1"), _m("2")], True, remaining_s=45.0) == 0


def test_priority_does_not_cut_ahead_of_user_song_already_at_front():
    """queue[0] 已是真人點歌 → 照 FIFO 排在它後面，不插隊別人。"""
    assert user_song_insert_index([_u("A", "1"), _m("2")], True, remaining_s=100) == 1


def test_priority_second_requester_lines_up_after_first():
    """A 已經插到最前（queue=[A, marvin]），B 接著點 → 排在 A 後面、Marvin 前面。"""
    assert user_song_insert_index([_u("A", "1"), _m("2")], True, remaining_s=100) == 1


def test_priority_disabled_via_env(monkeypatch):
    monkeypatch.setenv("MARVIN_REQUEST_PRIORITY", "0")
    assert user_song_insert_index([_m("1"), _m("2")], True, remaining_s=100) == 1


def test_priority_non_stream_mode_unaffected_by_remaining():
    assert user_song_insert_index([_u("a", "1"), _m("2")], False, remaining_s=100) == 1
    assert user_song_insert_index([_m("1")], False, remaining_s=100) == 0


# ── 整合：MusicCog._queue_user_song 實際落地插隊 ─────────────────────────────

def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.music_memory = None
    from cogs.music_cog import MusicCog
    return MusicCog(bot)


def test_integration_user_song_jumps_to_front_when_remaining_enough():
    cog = _make_cog()
    cog.stream_mode = True
    cog._current_stream_info = {"duration": 240}
    cog._current_stream_start_time = time.time() - 60  # 剩 180s
    cog.stream_queue = [_m("auto1")]
    cog._queue_user_song(_u("jack", "A"))
    assert cog.stream_queue[0]["title"] == "A"


def test_integration_user_song_stays_behind_when_remaining_low():
    cog = _make_cog()
    cog.stream_mode = True
    cog._current_stream_info = {"duration": 240}
    cog._current_stream_start_time = time.time() - 230  # 剩 10s
    cog.stream_queue = [_m("auto1")]
    cog._queue_user_song(_u("jack", "A"))
    assert cog.stream_queue[1]["title"] == "A"
