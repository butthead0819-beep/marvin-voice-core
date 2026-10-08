"""MusicCommandsMixin.queue_current_album —「播放這張專輯」handler 本體（10/8）。

跟 /tour 整張巡禮不同：只挑 pick_album_followups 選出的曲目排進一般佇列
（不打斷別人點歌、不換 mode），每首都過 resolved_matches_track 守門。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_cog():
    from cogs.music_cog import MusicCog
    bot = MagicMock()
    cog = MusicCog(bot)
    cog.radio_mode = False
    vc = MagicMock()
    vc.active_text_channel = MagicMock()
    vc.active_text_channel.send = AsyncMock()
    vc._play_ack = AsyncMock()
    vc.play_tts = AsyncMock()
    cog._vc = MagicMock(return_value=vc)
    return cog, vc


@pytest.mark.asyncio
async def test_no_current_song_speaks_and_skips_fetch():
    cog, vc = _make_cog()
    cog._current_stream_info = None
    cog._fetch_album_tracks = AsyncMock()

    await cog.queue_current_album("狗與露")

    vc.play_tts.assert_awaited_once()
    assert "沒在放歌" in vc.play_tts.call_args.args[0]
    cog._fetch_album_tracks.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_album_speaks_and_skips_fetch():
    cog, vc = _make_cog()
    cog._current_stream_info = {"artist": "周杰倫", "title": "晴天"}  # 沒有 album
    cog._fetch_album_tracks = AsyncMock()

    await cog.queue_current_album("狗與露")

    vc.play_tts.assert_awaited_once()
    assert "查不到是哪張專輯" in vc.play_tts.call_args.args[0]
    cog._fetch_album_tracks.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_tracks_speaks_fallback():
    cog, vc = _make_cog()
    cog._current_stream_info = {"artist": "周杰倫", "album": "葉惠美", "track": "晴天"}
    cog._fetch_album_tracks = AsyncMock(return_value=[])

    await cog.queue_current_album("狗與露")

    vc.play_tts.assert_awaited_once()
    assert "查不到" in vc.play_tts.call_args.args[0]
    assert "葉惠美" in vc.play_tts.call_args.args[0]


@pytest.mark.asyncio
async def test_album_tour_active_rejects():
    cog, vc = _make_cog()
    cog._album_tour_reject = AsyncMock(return_value=True)
    cog._fetch_album_tracks = AsyncMock()

    await cog.queue_current_album("狗與露")

    cog._fetch_album_tracks.assert_not_awaited()
    vc.play_tts.assert_not_awaited()


@pytest.mark.asyncio
async def test_happy_path_queues_resolved_tracks_skips_mismatch(monkeypatch):
    cog, vc = _make_cog()
    cog._current_stream_info = {"artist": "周杰倫", "album": "葉惠美",
                                 "track": "晴天", "title": "晴天"}
    tracks = ["七里香", "東風破", "完全不對的歌", "止戰之殤"]
    cog._fetch_album_tracks = AsyncMock(return_value=["晴天"] + tracks)
    cog._queue_user_song = MagicMock()
    cog._ensure_stream_loop = MagicMock()
    cog._resolve_yt_query = AsyncMock(side_effect=lambda q: {"title": q})

    import audiophile_fetcher as af
    monkeypatch.setattr(
        af, "resolved_matches_track",
        lambda info, track: track != "完全不對的歌",
    )

    await cog.queue_current_album("狗與露")

    assert cog._queue_user_song.call_count == 3
    for call in cog._queue_user_song.call_args_list:
        info = call.args[0]
        assert info["requested_by"] == "狗與露"
    cog._ensure_stream_loop.assert_called_once()
    vc.play_tts.assert_awaited_once()
    assert "葉惠美" in vc.play_tts.call_args.args[0]
