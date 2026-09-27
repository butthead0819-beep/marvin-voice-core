"""TDD — 歌曲開播 + 開頭 DJ Mix 口白補寫 marvin_speech.log。

規則：只呼叫真的 method（`_new_song_start_future`），不把它的邏輯複製進測試裡驗算自己。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_mixin_obj():
    """MusicTailDJMixin 是給 MusicCog 用的 mixin，這裡建一個只帶這個 mixin 的最小物件，
    避免拉整個 MusicCog（含 discord.py Cog 初始化）。"""
    from cogs.music_cog_tail_dj import MusicTailDJMixin

    class _Obj(MusicTailDJMixin):
        pass

    return _Obj()


# ── _new_song_start_future ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_dj_audio_logs_song_start_only():
    obj = _make_mixin_obj()
    info = {"title": "七里香"}

    with patch("cogs.music_cog_tail_dj.log_song_start") as mock_song, \
         patch("cogs.music_cog_tail_dj.log_marvin_speech") as mock_dj:
        fut = obj._new_song_start_future(info, None)
        fut.set_result(123.0)
        await asyncio.sleep(0)

    mock_song.assert_called_once_with("七里香", start_ts=123.0, artist=None)
    mock_dj.assert_not_called()


@pytest.mark.asyncio
async def test_dj_audio_matches_prefetch_logs_dj_speech(tmp_path):
    obj = _make_mixin_obj()
    p = str(tmp_path / "dj.mp3")
    with open(p, "wb") as f:
        f.write(b"x")
    info = {"title": "七里香", "_dj_prefetch": {"audio_path": p, "text": "DJ台詞"}}

    with patch("cogs.music_cog_tail_dj.log_song_start") as mock_song, \
         patch("cogs.music_cog_tail_dj.log_marvin_speech") as mock_dj:
        fut = obj._new_song_start_future(info, p)
        fut.set_result(123.0)
        await asyncio.sleep(0)

    mock_song.assert_called_once_with("七里香", start_ts=123.0, artist=None)
    mock_dj.assert_called_once_with("DJ台詞", start_ts=123.0, layer=1, voice=None, src="dj")


@pytest.mark.asyncio
async def test_dj_audio_with_clip_suffix_still_matches(tmp_path):
    obj = _make_mixin_obj()
    p = str(tmp_path / "dj.mp3")
    with open(p, "wb") as f:
        f.write(b"x")
    with_clip = f"{p}.with_clip.wav"
    with open(with_clip, "wb") as f:
        f.write(b"y")
    info = {"title": "七里香", "_dj_prefetch": {"audio_path": p, "text": "DJ台詞"}}

    with patch("cogs.music_cog_tail_dj.log_song_start"), \
         patch("cogs.music_cog_tail_dj.log_marvin_speech") as mock_dj:
        fut = obj._new_song_start_future(info, with_clip)
        fut.set_result(123.0)
        await asyncio.sleep(0)

    mock_dj.assert_called_once_with("DJ台詞", start_ts=123.0, layer=1, voice=None, src="dj")


@pytest.mark.asyncio
async def test_dj_audio_mismatched_prefetch_path_does_not_log_dj(tmp_path):
    obj = _make_mixin_obj()
    p = str(tmp_path / "dj.mp3")
    other = str(tmp_path / "other.mp3")
    for path in (p, other):
        with open(path, "wb") as f:
            f.write(b"x")
    info = {"title": "七里香", "_dj_prefetch": {"audio_path": other, "text": "DJ台詞"}}

    with patch("cogs.music_cog_tail_dj.log_song_start") as mock_song, \
         patch("cogs.music_cog_tail_dj.log_marvin_speech") as mock_dj:
        fut = obj._new_song_start_future(info, p)
        fut.set_result(123.0)
        await asyncio.sleep(0)

    mock_song.assert_called_once()
    mock_dj.assert_not_called()


@pytest.mark.asyncio
async def test_dj_audio_path_not_exists_does_not_log_dj(tmp_path):
    obj = _make_mixin_obj()
    p = str(tmp_path / "does_not_exist.mp3")
    info = {"title": "七里香", "_dj_prefetch": {"audio_path": p, "text": "DJ台詞"}}

    with patch("cogs.music_cog_tail_dj.log_song_start") as mock_song, \
         patch("cogs.music_cog_tail_dj.log_marvin_speech") as mock_dj:
        fut = obj._new_song_start_future(info, p)
        fut.set_result(123.0)
        await asyncio.sleep(0)

    mock_song.assert_called_once()
    mock_dj.assert_not_called()


@pytest.mark.asyncio
async def test_future_cancelled_logs_nothing():
    obj = _make_mixin_obj()
    info = {"title": "七里香"}

    with patch("cogs.music_cog_tail_dj.log_song_start") as mock_song, \
         patch("cogs.music_cog_tail_dj.log_marvin_speech") as mock_dj:
        fut = obj._new_song_start_future(info, None)
        fut.cancel()
        await asyncio.sleep(0)

    mock_song.assert_not_called()
    mock_dj.assert_not_called()


@pytest.mark.asyncio
async def test_artist_passed_through_to_log_song_start():
    obj = _make_mixin_obj()
    info = {"title": "七里香", "artist": "周杰倫"}

    with patch("cogs.music_cog_tail_dj.log_song_start") as mock_song, \
         patch("cogs.music_cog_tail_dj.log_marvin_speech"):
        fut = obj._new_song_start_future(info, None)
        fut.set_result(123.0)
        await asyncio.sleep(0)

    mock_song.assert_called_once_with("七里香", start_ts=123.0, artist="周杰倫")


# ── _fetch_song_meta 寫 _dj_prefetch ─────────────────────────────────────────

def _make_music_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.music_memory = MagicMock()
    bot.music_memory.all_songs = MagicMock(return_value={})

    from cogs.music_cog import MusicCog
    return MusicCog(bot)


@pytest.mark.asyncio
async def test_fetch_song_meta_writes_dj_prefetch_when_audio_and_text_present():
    cog = _make_music_cog()
    cog._fetch_lyrics_raw = AsyncMock(return_value=None)
    cog._fetch_comment_raw = AsyncMock(return_value=None)
    cog._fetch_dj_interjection_raw = AsyncMock(
        return_value={"text": "DJ台詞", "audio_path": "/tmp/dj.mp3"}
    )
    cog._fetch_lyrics_synced = AsyncMock(return_value=None)

    info = {"title": "七里香", "url": "x"}
    await cog._fetch_song_meta(info)

    assert info["_dj_prefetch"] == {"audio_path": "/tmp/dj.mp3", "text": "DJ台詞"}


@pytest.mark.asyncio
async def test_fetch_song_meta_no_dj_prefetch_when_audio_path_missing():
    cog = _make_music_cog()
    cog._fetch_lyrics_raw = AsyncMock(return_value=None)
    cog._fetch_comment_raw = AsyncMock(return_value=None)
    cog._fetch_dj_interjection_raw = AsyncMock(
        return_value={"text": "DJ台詞", "audio_path": None}
    )
    cog._fetch_lyrics_synced = AsyncMock(return_value=None)

    info = {"title": "七里香", "url": "x"}
    await cog._fetch_song_meta(info)

    assert "_dj_prefetch" not in info
