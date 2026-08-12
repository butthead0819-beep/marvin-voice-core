"""TDD: MusicCog 歌單匯出與匯入 Discord 斜線指令測試。"""
from __future__ import annotations

import io
import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest


def _make_interaction(username="小明"):
    inter = MagicMock()
    inter.user.display_name = username
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock()
    return inter


@pytest.fixture
def music_cog(tmp_path):
    from music_memory import MusicMemory
    from cogs.music_cog import MusicCog

    mm = MusicMemory(path=str(tmp_path / "mm.json"))
    bot = MagicMock()
    bot.music_memory = mm
    cog = MusicCog(bot)
    return cog


# ── /marvin_playlist_export ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_playlist_export_empty(music_cog):
    inter = _make_interaction("小明")
    await music_cog.marvin_playlist_export.callback(music_cog, inter, format="txt")

    inter.response.defer.assert_awaited_once()
    inter.followup.send.assert_awaited_once()
    msg = inter.followup.send.call_args[0][0]
    assert "找不到" in msg or "沒有" in msg


@pytest.mark.asyncio
async def test_playlist_export_with_songs(music_cog):
    mm = music_cog.bot.music_memory
    mm.record_play({"title": "稻香", "uploader": "周杰倫", "webpage_url": "https://youtu.be/xxx"}, "小明")

    inter = _make_interaction("小明")
    await music_cog.marvin_playlist_export.callback(music_cog, inter, format="txt")

    inter.followup.send.assert_awaited_once()
    args, kwargs = inter.followup.send.call_args
    assert "小明" in args[0]
    assert "file" in kwargs
    file = kwargs["file"]
    assert file.filename.endswith(".txt")


@pytest.mark.asyncio
async def test_playlist_export_target_user(music_cog):
    mm = music_cog.bot.music_memory
    mm.record_play({"title": "夜曲", "uploader": "周杰倫", "webpage_url": "https://youtu.be/yyy"}, "小華")

    inter = _make_interaction("小明")
    await music_cog.marvin_playlist_export.callback(music_cog, inter, format="json", target_user="小華")

    inter.followup.send.assert_awaited_once()
    args, kwargs = inter.followup.send.call_args
    assert "小華" in args[0]
    assert kwargs["file"].filename.endswith(".json")


# ── /marvin_playlist_import ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_playlist_import_no_input(music_cog):
    inter = _make_interaction("小明")
    await music_cog.marvin_playlist_import.callback(music_cog, inter, query_or_url=None, file=None)

    inter.followup.send.assert_awaited_once()
    msg = inter.followup.send.call_args[0][0]
    assert "請提供" in msg or "無效" in msg


@pytest.mark.asyncio
async def test_playlist_import_youtube_playlist(music_cog):
    inter = _make_interaction("小明")
    playlist_url = "https://www.youtube.com/playlist?list=PL12345"

    sample_yt_songs = [
        {"title": "歌A", "uploader": "歌手A", "webpage_url": "https://youtu.be/a"},
        {"title": "歌B", "uploader": "歌手B", "webpage_url": "https://youtu.be/b"},
    ]

    with patch("cogs.music_cog.extract_youtube_playlist_flat", new=AsyncMock(return_value=sample_yt_songs)):
        await music_cog.marvin_playlist_import.callback(
            music_cog, inter, query_or_url=playlist_url, file=None
        )

    inter.followup.send.assert_awaited_once()
    msg = inter.followup.send.call_args[0][0]
    assert "成功" in msg
    assert "2" in msg

    # 驗證真的寫入 memory
    exported = music_cog.bot.music_memory.export_user_playlist("小明")
    assert len(exported) == 2


@pytest.mark.asyncio
async def test_playlist_import_from_file_attachment(music_cog):
    inter = _make_interaction("小明")
    file_attachment = MagicMock()
    file_attachment.filename = "my_playlist.json"
    file_attachment.read = AsyncMock(return_value=json.dumps([
        {"title": "自訂歌1", "uploader": "歌手1", "webpage_url": "https://youtu.be/1"},
        {"title": "自訂歌2", "uploader": "歌手2", "webpage_url": "https://youtu.be/2"},
    ]).encode("utf-8"))

    await music_cog.marvin_playlist_import.callback(
        music_cog, inter, query_or_url=None, file=file_attachment
    )

    inter.followup.send.assert_awaited_once()
    msg = inter.followup.send.call_args[0][0]
    assert "成功" in msg
    assert "2" in msg

    exported = music_cog.bot.music_memory.export_user_playlist("小明")
    assert len(exported) == 2


@pytest.mark.asyncio
async def test_playlist_import_from_text_query(music_cog):
    inter = _make_interaction("小明")
    raw_query = "1. 周杰倫 - 晴天 (https://youtu.be/qt)\n2. 告五人 - 披星戴月的想你"

    await music_cog.marvin_playlist_import.callback(
        music_cog, inter, query_or_url=raw_query, file=None
    )

    inter.followup.send.assert_awaited_once()
    msg = inter.followup.send.call_args[0][0]
    assert "成功" in msg
    assert "2" in msg
