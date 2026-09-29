"""歌曲卡（Discord embed + 封面合成圖）曲名/歌手用曲庫正規化資料（canon::<videoId>）。

2026-09-29 使用者：曲庫更新後，文字歌曲卡的曲名及歌手要用正規化後的資料寫。
沒有正規化資料時退回 YouTube 原標題（行為不變）。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs.voice_views import build_song_embed, song_display_title

VID = "abcdefghijk"
CANON = {"artist": "鄧麗君", "title": "月亮代表我的心", "album": "永恆", "year": 1990, "source": "shazam+itunes"}


def test_display_title_uses_canon_title_and_artist():
    info = {"title": "鄧麗君 -月亮代表我的心 Teresa Teng (HD) (with lyrics)", "_canon": CANON}
    assert song_display_title(info) == "月亮代表我的心 - 鄧麗君"


def test_display_title_falls_back_to_raw_title_without_canon():
    assert song_display_title({"title": "髒標題 Official MV"}) == "髒標題 Official MV"
    assert song_display_title({"title": "髒標題", "_canon": {"title": "", "artist": "X"}}) == "髒標題"


def test_song_embed_title_uses_canon():
    embed = build_song_embed({"title": "髒標題【動態歌詞】", "_canon": CANON,
                              "webpage_url": f"https://www.youtube.com/watch?v={VID}"})
    assert embed.title == "月亮代表我的心 - 鄧麗君"


def test_song_embed_without_canon_unchanged():
    assert build_song_embed({"title": "髒標題【動態歌詞】"}).title == "髒標題【動態歌詞】"


def _make_cog(store):
    from tests.test_dj_story_context import _make_cog as _base
    cog = _base()
    cog._audiophile_deps = MagicMock(return_value=(store, MagicMock(), cog.bot.router))
    return cog


def _store(data):
    s = MagicMock()
    s.get = MagicMock(side_effect=lambda k: data.get(k))
    return s


def test_attach_cached_canon_reads_store_by_video_id():
    cog = _make_cog(_store({f"canon::{VID}": CANON}))
    info = {"title": "髒", "webpage_url": f"https://www.youtube.com/watch?v={VID}"}
    cog._attach_cached_canon(info)
    assert info["_canon"] == CANON


def test_attach_cached_canon_keeps_existing_and_tolerates_miss():
    cog = _make_cog(_store({}))
    info = {"title": "髒", "webpage_url": f"https://www.youtube.com/watch?v={VID}"}
    cog._attach_cached_canon(info)
    assert "_canon" not in info
    other = {"artist": "A", "title": "B"}
    info2 = {"title": "髒", "_canon": other, "webpage_url": f"https://www.youtube.com/watch?v={VID}"}
    _make_cog(_store({f"canon::{VID}": CANON}))._attach_cached_canon(info2)
    assert info2["_canon"] is other


@pytest.mark.asyncio
async def test_post_music_cards_embed_title_uses_cached_canon(monkeypatch):
    """歌曲卡貼文時 info 還沒帶 _canon（例：這首是之前播過、已正規化的歌）→ 從快取補上再組卡。"""
    import cogs.voice_views as vv
    cog = _make_cog(_store({f"canon::{VID}": CANON}))
    cog._fetch_lyrics_control_card = AsyncMock(return_value=None)
    monkeypatch.setattr(vv, "PlayControlView", MagicMock())
    monkeypatch.setattr(vv, "build_control_embed", MagicMock(return_value=MagicMock()))
    ch = MagicMock()
    ch.send = AsyncMock()
    info = {"title": "鄧麗君 -月亮代表我的心 (HD)", "requested_by": "大肚",
            "webpage_url": f"https://www.youtube.com/watch?v={VID}"}
    await cog._post_music_cards(ch, MagicMock(), info)
    first_embed = ch.send.await_args_list[0].kwargs["embed"]
    assert first_embed.title == "月亮代表我的心 - 鄧麗君"


@pytest.mark.asyncio
async def test_dj_song_material_attaches_canon_to_info(tmp_path, monkeypatch):
    """DJ prefetch 正規化完就掛上 info，之後這首開播貼卡直接用。"""
    import audiophile_fetcher
    cog = _make_cog(MagicMock())
    monkeypatch.setattr(audiophile_fetcher, "resolve_canon", AsyncMock(return_value=CANON))
    monkeypatch.setattr(audiophile_fetcher, "song_guide_for_dj", AsyncMock(return_value=None))
    info = {"title": "髒", "requested_by": "大肚", "webpage_url": f"https://www.youtube.com/watch?v={VID}"}
    await cog._dj_song_material(info, "月亮代表我的心", "鄧麗君")
    assert info["_canon"] == CANON
