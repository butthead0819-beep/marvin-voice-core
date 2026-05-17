"""
TDD：_resolve_yt_query 應該先打 YouTube Music 搜尋（ytmsearch5），
無結果再 fallback 到一般 YouTube 搜尋（ytsearch5）。

理由：使用者抱怨「人工去 YouTube Music 找得到的歌，叫馬文播卻回報找不到」。
原因是 ytsearch5 跟 YouTube Music 是不同的 catalog，冷門歌或重新上傳版
在 YT Music 有但一般 YouTube 搜尋會 0 命中，導致 entries=[] → 回 None
→ 馬文講「找不到」。
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.router = MagicMock()
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.post_summon_callback = None

    with patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        cog = VoiceController(bot)
    cog.stt_logger = MagicMock()
    return cog


class _FakeYDL:
    """Mock yt_dlp.YoutubeDL — 紀錄被打過的搜尋字串，按腳本回 entries。"""

    def __init__(self, ydl_opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    # script: {search_prefix: entries_list_or_exception}
    script: dict = {}
    calls: list = []

    def extract_info(self, search, download=False):
        type(self).calls.append(search)
        for prefix, response in type(self).script.items():
            if search.startswith(prefix):
                if isinstance(response, Exception):
                    raise response
                return {"entries": response}
        return {"entries": []}


def _music_entry(title="Test Song", url="http://stream.test/song"):
    return {
        "title": title,
        "uploader": "Artist - Topic",
        "url": url,
        "categories": ["Music"],
        "duration": 200,
        "webpage_url": f"https://youtube.com/watch?v=xxx",
        "thumbnail": "t.png",
    }


@pytest.mark.asyncio
async def test_resolve_yt_query_tries_ytmsearch_first():
    """搜尋時應該先打 ytmsearch5:{query}，不該優先用 ytsearch5。"""
    cog = _make_cog()
    _FakeYDL.calls = []
    _FakeYDL.script = {
        "ytmsearch5:": [_music_entry("YT Music Hit")],
        "ytsearch5:": [_music_entry("Regular YT Hit")],
    }

    with patch("yt_dlp.YoutubeDL", _FakeYDL):
        info = await cog._resolve_yt_query("陶喆 普通朋友")

    assert info is not None
    assert info["title"] == "YT Music Hit"
    assert _FakeYDL.calls[0].startswith("ytmsearch5:"), \
        f"第一次搜尋應是 ytmsearch5，實際是 {_FakeYDL.calls[0]}"


@pytest.mark.asyncio
async def test_resolve_yt_query_falls_back_to_ytsearch_when_ytm_empty():
    """ytmsearch5 0 命中時，應自動 fallback 到 ytsearch5。"""
    cog = _make_cog()
    _FakeYDL.calls = []
    _FakeYDL.script = {
        "ytmsearch5:": [],  # YT Music 沒結果
        "ytsearch5:": [_music_entry("Fallback Hit")],
    }

    with patch("yt_dlp.YoutubeDL", _FakeYDL):
        info = await cog._resolve_yt_query("冷門歌 only on regular YT")

    assert info is not None
    assert info["title"] == "Fallback Hit"
    # 兩個 prefix 都被打過
    prefixes = [c.split(":")[0] + ":" for c in _FakeYDL.calls]
    assert "ytmsearch5:" in prefixes
    assert "ytsearch5:" in prefixes


@pytest.mark.asyncio
async def test_resolve_yt_query_returns_none_when_both_empty():
    """兩個搜尋都 0 命中時，回 None，讓上層回報找不到。"""
    cog = _make_cog()
    _FakeYDL.calls = []
    _FakeYDL.script = {
        "ytmsearch5:": [],
        "ytsearch5:": [],
    }

    with patch("yt_dlp.YoutubeDL", _FakeYDL):
        info = await cog._resolve_yt_query("zzz random nonsense xyz123")

    assert info is None


@pytest.mark.asyncio
async def test_resolve_yt_query_url_uses_direct_extract():
    """URL 查詢不該走 ytmsearch / ytsearch，要直接 extract_info(url)。"""
    cog = _make_cog()
    _FakeYDL.calls = []

    class _DirectYDL(_FakeYDL):
        def extract_info(self, search, download=False):
            type(self).calls.append(search)
            # 模擬 URL 直接解析的回傳：單一影片 info，不是搜尋結果
            return {
                "title": "Direct URL Song",
                "url": "http://stream.direct/x",
                "uploader": "Channel",
                "categories": ["Music"],
                "duration": 200,
                "webpage_url": search,
                "thumbnail": "t.png",
            }

    with patch("yt_dlp.YoutubeDL", _DirectYDL):
        info = await cog._resolve_yt_query("https://youtu.be/abc123")

    assert info is not None
    assert info["title"] == "Direct URL Song"
    assert _DirectYDL.calls == ["https://youtu.be/abc123"], \
        f"URL 應該直接打，不該加 ytmsearch/ytsearch 前綴：{_DirectYDL.calls}"
