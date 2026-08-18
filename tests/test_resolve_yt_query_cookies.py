"""TDD：_resolve_yt_query() 接上 YouTube cookies 繞開 IP 節流（2026-08-17/18
連續多天 403 Forbidden，見 incident_youtube_403_ip_throttle_2026-08-17 記憶；
2026-08-18 實測登入身分的請求能繞過，匿名 ANDROID_VR client 不支援帶 cookies，
改用一般 client + cookies + remote_components=['ejs:github'] 解 JS challenge）。

cookies.txt 檔案不存在（沒匯出過）就整段跳過、退回原本匿名解析，零行為改變
——這是這裡測的重點，不測真的打 network。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="key")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="深夜")

    from cogs.music_cog import MusicCog
    return MusicCog(bot)


@pytest.mark.asyncio
async def test_resolve_yt_query_adds_cookiefile_when_file_exists():
    cog = _make_cog()
    captured_opts = {}

    class _FakeYDL:
        def __init__(self, opts):
            captured_opts.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, query, download=False):
            return {"url": "https://cdn.example/x", "title": "測試", "webpage_url": query}

    with patch("cogs.music_cog.os.path.exists", return_value=True), \
         patch("cogs.music_cog.yt_dlp.YoutubeDL", _FakeYDL):
        await cog._resolve_yt_query("https://www.youtube.com/watch?v=abc123")

    assert captured_opts.get("cookiefile") == cog_module_cookies_path()
    assert captured_opts.get("remote_components") == ["ejs:github"]


@pytest.mark.asyncio
async def test_resolve_yt_query_skips_cookiefile_when_absent():
    """cookies.txt 不存在（使用者沒匯出過）→ 完全不帶 cookiefile/remote_components，
    退回原本匿名 ANDROID_VR-friendly 解析路徑，零行為改變。"""
    cog = _make_cog()
    captured_opts = {}

    class _FakeYDL:
        def __init__(self, opts):
            captured_opts.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, query, download=False):
            return {"url": "https://cdn.example/x", "title": "測試", "webpage_url": query}

    with patch("cogs.music_cog.os.path.exists", return_value=False), \
         patch("cogs.music_cog.yt_dlp.YoutubeDL", _FakeYDL):
        await cog._resolve_yt_query("https://www.youtube.com/watch?v=abc123")

    assert "cookiefile" not in captured_opts
    assert "remote_components" not in captured_opts


def cog_module_cookies_path():
    from cogs.music_cog import _YT_COOKIES_FILE
    return _YT_COOKIES_FILE
