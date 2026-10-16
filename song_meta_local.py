"""點歌補 metadata 前查本地正規化資料（純函式，無 IO）。

Why：直接點 URL（含語音 WakeShortcut 轉出的 YouTube URL）時沒有使用者原始
文字查詢，iTunes/Spotify 只能用髒的 YT 標題＋uploader 查，常配不到。本地已有
兩份正規化資料可先查：`music_memory.all_songs()` 的 spotify_* 欄位（約 20%
覆蓋，資料最乾淨）、`song_name_clean._default_catalog_index()` 的 videoId→
"藝人 歌名" 字串（547 筆，不可拆成藝人/歌名，只能整串當查詢文字）。

優先序（見 cogs/music_cog.py::_local_song_meta 呼叫處）：
查詢文字＝使用者原文 > 本地（spotify 組字 / catalog 名稱）> YT 標題；
artist/album＝本地 spotify > iTunes > 不寫（ICY/AVRCP 退回 uploader）。
"""
from __future__ import annotations

from music_memory import extract_video_id


def local_meta_lookup(webpage_url: str | None, songs: dict, catalog_index: dict) -> dict:
    """依序查本地正規化資料，回傳可用的欄位（查不到回 {}）。

    - `songs[webpage_url]` 有非空 `spotify_artist` →
      `{"artist", "album"（或 None), "spotify": {...}, "query"}`。
    - 否則 `extract_video_id(webpage_url)` 命中 `catalog_index` → `{"query": ...}`。
    - 否則 `{}`。
    """
    if not webpage_url or not songs or not isinstance(songs, dict):
        song = None
    else:
        song = songs.get(webpage_url)

    if isinstance(song, dict):
        spotify_artist = song.get("spotify_artist")
        if spotify_artist:
            spotify_title = song.get("spotify_title")
            spotify_album = song.get("spotify_album")
            spotify_uri = song.get("spotify_uri")
            return {
                "artist": spotify_artist,
                "album": spotify_album or None,
                "spotify": {
                    "title": spotify_title,
                    "artist": spotify_artist,
                    "album": spotify_album,
                    "uri": spotify_uri,
                },
                "query": f"{spotify_artist} {spotify_title or ''}".strip(),
            }

    if catalog_index and isinstance(catalog_index, dict):
        vid = extract_video_id(webpage_url or "")
        if vid and vid in catalog_index:
            return {"query": catalog_index[vid]}

    return {}
