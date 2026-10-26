"""music_memory.recent_requested_titles — 點播紀錄單一來源（取代 suki_memory.song_history）。

某人點過哪些歌只看 music_memory 的 songs[*].requesters / plays，不再有第二份副本。
"""
from __future__ import annotations

from music_memory import MusicMemory, recent_requested_titles


def _song(title, requesters, plays=None):
    return {"title": title, "requesters": requesters, "plays": plays or []}


def test_requesters_missing_user_not_included_even_with_plays():
    songs = {
        "u1": _song("歌A", {}, plays=[{"by": "showay", "ts": 100}]),
    }
    assert recent_requested_titles(songs, "showay") == []


def test_marvin_recommended_only_not_included():
    songs = {
        "u1": _song("歌A", {"Marvin推薦（為showay）": 3}),
    }
    assert recent_requested_titles(songs, "showay") == []


def test_sorted_by_last_play_ts_descending():
    songs = {
        "u1": _song("舊歌", {"showay": 1}, plays=[{"by": "showay", "ts": 100}]),
        "u2": _song("新歌", {"showay": 1}, plays=[{"by": "showay", "ts": 300}]),
        "u3": _song("中間歌", {"showay": 1}, plays=[{"by": "showay", "ts": 200}]),
    }
    assert recent_requested_titles(songs, "showay") == ["新歌", "中間歌", "舊歌"]


def test_limit_applies_and_none_returns_all():
    songs = {
        "u1": _song("歌A", {"showay": 1}, plays=[{"by": "showay", "ts": 1}]),
        "u2": _song("歌B", {"showay": 1}, plays=[{"by": "showay", "ts": 2}]),
        "u3": _song("歌C", {"showay": 1}, plays=[{"by": "showay", "ts": 3}]),
    }
    assert recent_requested_titles(songs, "showay", limit=2) == ["歌C", "歌B"]
    assert recent_requested_titles(songs, "showay", limit=None) == ["歌C", "歌B", "歌A"]


def test_empty_title_skipped_and_duplicate_titles_deduped_keeping_newer():
    songs = {
        "u1": _song("", {"showay": 1}, plays=[{"by": "showay", "ts": 50}]),
        "u2": _song("重複歌", {"showay": 1}, plays=[{"by": "showay", "ts": 10}]),
        "u3": _song("重複歌", {"showay": 1}, plays=[{"by": "showay", "ts": 90}]),
    }
    titles = recent_requested_titles(songs, "showay")
    assert titles == ["重複歌"]


def test_music_memory_method_uses_same_logic(tmp_path):
    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    mm._data["songs"] = {
        "u1": _song("舊歌", {"showay": 1}, plays=[{"by": "showay", "ts": 1}]),
        "u2": _song("新歌", {"showay": 1}, plays=[{"by": "showay", "ts": 2}]),
    }
    assert mm.get_recent_requested_titles("showay") == ["新歌", "舊歌"]
    assert mm.get_recent_requested_titles("showay", limit=1) == ["新歌"]
