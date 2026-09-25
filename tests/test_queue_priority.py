"""TDD: queue_priority.user_song_insert_index（自 music_cog 搬出，行為不變 → 補真人點歌優先）。"""
from __future__ import annotations

from queue_priority import user_song_insert_index


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
