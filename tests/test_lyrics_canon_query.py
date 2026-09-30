"""9/30：歌詞用 YouTube 髒標題查常抓不到（近 25 首約一半），改先用正規化曲名（_canon，iTunes）查，
查不到再退原本解析的標題。實測原本抓不到的 13 首，有正規化快取的 11 首救回 10 首。"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _cog():
    from cogs.music_cog import MusicCog
    cog = MusicCog.__new__(MusicCog)
    cog.bot = MagicMock()
    return cog


_INFO = {"title": "張惠妹 A-Mei《哭不出來》官方MV", "uploader": "aMEI", "duration": 250,
         "_canon": {"title": "哭不出來", "artist": "張惠妹"}}


def test_query_pairs_canon_first_then_raw():
    cog = _cog()
    pairs = cog._lyrics_query_pairs(dict(_INFO))
    assert pairs[0] == ("哭不出來", "張惠妹")
    assert pairs[1] == cog._parse_song_title_artist(_INFO)


def test_query_pairs_without_canon_is_raw_only():
    cog = _cog()
    info = {k: v for k, v in _INFO.items() if k != "_canon"}
    assert cog._lyrics_query_pairs(info) == [cog._parse_song_title_artist(info)]


@pytest.mark.asyncio
async def test_fetch_lyrics_raw_uses_canon_query_first(monkeypatch):
    import syncedlyrics
    seen = []

    def _search(q, providers=None):
        seen.append(q)
        return "[00:01.00]哭不出來的歌詞" if q == "哭不出來 張惠妹" else None

    monkeypatch.setattr(syncedlyrics, "search", _search)
    out = await _cog()._fetch_lyrics_raw(dict(_INFO))
    assert out == "哭不出來的歌詞"
    assert seen[0] == "哭不出來 張惠妹"


@pytest.mark.asyncio
async def test_fetch_lyrics_raw_falls_back_to_raw_title(monkeypatch):
    import syncedlyrics
    seen = []

    def _search(q, providers=None):
        seen.append(q)
        return None if q == "哭不出來 張惠妹" else "[00:01.00]退回原標題抓到"

    monkeypatch.setattr(syncedlyrics, "search", _search)
    out = await _cog()._fetch_lyrics_raw(dict(_INFO))
    assert out == "退回原標題抓到"
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_fetch_lyrics_synced_uses_canon_query_first(monkeypatch):
    import syncedlyrics
    seen = []

    def _search(q, providers=None):
        seen.append(q)
        return "[00:01.00]同步歌詞" if q == "哭不出來 張惠妹" else None

    monkeypatch.setattr(syncedlyrics, "search", _search)
    out = await _cog()._fetch_lyrics_synced(dict(_INFO))
    assert out == "[00:01.00]同步歌詞"
    assert seen[0] == "哭不出來 張惠妹"


@pytest.mark.asyncio
async def test_fetch_song_meta_attaches_cached_canon_before_lyrics(monkeypatch):
    """_fetch_song_meta 開始抓歌詞前先掛上本地快取的正規化曲名（零網路），歌詞查詢才用得到。"""
    from unittest.mock import AsyncMock
    cog = _cog()
    seen = {}

    def _attach(info):
        info["_canon"] = {"title": "哭不出來", "artist": "張惠妹"}

    async def _lyrics(info):
        seen["canon"] = info.get("_canon")
        return None

    cog._attach_cached_canon = MagicMock(side_effect=_attach)
    cog._fetch_lyrics_raw = _lyrics
    cog._fetch_comment_raw = AsyncMock(return_value=None)
    cog._fetch_dj_interjection_raw = AsyncMock(return_value=None)
    cog._fetch_lyrics_synced = AsyncMock(return_value=None)
    info = {"title": "x", "url": "https://www.youtube.com/watch?v=abc"}
    await cog._fetch_song_meta(info)
    assert seen["canon"] == {"title": "哭不出來", "artist": "張惠妹"}
