"""黑箱測 song_meta_local.local_meta_lookup——不複製被測邏輯。"""
from __future__ import annotations

from song_meta_local import local_meta_lookup

_URL = "https://www.youtube.com/watch?v=tER-0RhdAow"
_URL_SHORT = "https://youtu.be/tER-0RhdAow"


def test_spotify_hit_returns_artist_album_and_query():
    songs = {
        _URL: {
            "spotify_title": "左邊的人",
            "spotify_artist": "陳華",
            "spotify_album": "左邊的人",
            "spotify_uri": "spotify:track:x",
        }
    }
    result = local_meta_lookup(_URL, songs, {})
    assert result["artist"] == "陳華"
    assert result["album"] == "左邊的人"
    assert result["spotify"] == {
        "title": "左邊的人",
        "artist": "陳華",
        "album": "左邊的人",
        "uri": "spotify:track:x",
    }
    assert result["query"] == "陳華 左邊的人"


def test_spotify_hit_with_no_album_returns_none_album():
    songs = {
        _URL: {
            "spotify_title": "左邊的人",
            "spotify_artist": "陳華",
            "spotify_album": "",
            "spotify_uri": "spotify:track:x",
        }
    }
    result = local_meta_lookup(_URL, songs, {})
    assert result["album"] is None
    assert result["spotify"]["album"] == ""


def test_catalog_hit_full_url():
    idx = {"tER-0RhdAow": "陳華 左邊的人"}
    result = local_meta_lookup(_URL, {}, idx)
    assert result == {"query": "陳華 左邊的人"}


def test_catalog_hit_short_url():
    idx = {"tER-0RhdAow": "陳華 左邊的人"}
    result = local_meta_lookup(_URL_SHORT, {}, idx)
    assert result == {"query": "陳華 左邊的人"}


def test_spotify_artist_empty_falls_back_to_catalog():
    songs = {_URL: {"spotify_title": "x", "spotify_artist": "", "spotify_album": ""}}
    idx = {"tER-0RhdAow": "陳華 左邊的人"}
    result = local_meta_lookup(_URL, songs, idx)
    assert result == {"query": "陳華 左邊的人"}


def test_nothing_found_returns_empty_dict():
    result = local_meta_lookup(_URL, {}, {})
    assert result == {}


def test_none_inputs_do_not_crash():
    assert local_meta_lookup(None, None, None) == {}
    assert local_meta_lookup("", {"a": 1}, {"b": 2}) == {}
