"""使用者自選曲 LIFO 插隊（2026-06-04）。

自選曲（手動點 / 語音點歌）要插到待播一（下一首就播）、且 LIFO（最近點的先播），
一律排在 auto-recommend（Marvin ambient，append 在尾）之前。
_stream_loop 用 pop(0) 取歌，故 insert(0) = 下一首、不打斷正在播的。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.post_summon_callback = None
    with patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        cog = VoiceController(bot)
    cog.stt_logger = MagicMock()
    return cog


def _song(t):
    return {"title": t, "url": f"http://x/{t}"}


def test_user_song_inserts_at_front():
    cog = _make_cog()
    cog.stream_queue = [_song("auto1"), _song("auto2")]   # 既有 auto-recommend
    cog._queue_user_song(_song("U1"))
    assert [s["title"] for s in cog.stream_queue] == ["U1", "auto1", "auto2"]


def test_user_songs_are_lifo():
    cog = _make_cog()
    cog.stream_queue = [_song("auto1")]
    cog._queue_user_song(_song("U1"))
    cog._queue_user_song(_song("U2"))   # 後點的 U2 應排在 U1 前（LIFO）
    assert [s["title"] for s in cog.stream_queue] == ["U2", "U1", "auto1"]


def test_user_song_into_empty_queue():
    cog = _make_cog()
    cog.stream_queue = []
    cog._queue_user_song(_song("U1"))
    assert [s["title"] for s in cog.stream_queue] == ["U1"]


def test_user_song_updates_t2_seed():
    """使用者點歌 → _last_user_song_seed 更新成那首的 video_id（T2 radio seed 跟著走）。"""
    cog = _make_cog()
    cog.stream_queue = []
    cog._queue_user_song({
        "title": "稻香", "url": "http://x", "requested_by": "alice",
        "webpage_url": "https://www.youtube.com/watch?v=ABCDEFGHIJK",
    })
    assert cog._last_user_song_seed == "ABCDEFGHIJK"
