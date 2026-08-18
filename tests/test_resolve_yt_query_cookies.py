"""TDD：_resolve_yt_query() 接上 YouTube cookies 繞開 IP 節流（2026-08-17/18
連續多天 403 Forbidden，見 incident_youtube_403_ip_throttle_2026-08-17 記憶；
2026-08-18 實測登入身分的請求能繞過，匿名 ANDROID_VR client 不支援帶 cookies，
改用一般 client + cookies + remote_components=['ejs:github'] 解 JS challenge）。

優先序：cookiesfrombrowser（永遠最新，需一次性 Keychain 授權）→ cookiefile
（使用者手動匯出，會過期）→ 無 cookies（原本匿名解析，零行為改變的最終退回）。
單一選定、不做「這個來源失敗就換下一個」的執行期重試——那樣會跟既有的
OSError errno=11 專屬重試邏輯混在一起，讓無關的例外也被重試多次（見
test_music_command_dedup.py::test_resolve_yt_query_does_not_retry_on_other_oserror
既有測試鎖住「非 errno=11 的 OSError 只該試一次」）。
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


def _cookies_path():
    from cogs.music_cog import _YT_COOKIES_FILE
    return _YT_COOKIES_FILE


class _FakeYDL:
    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, query, download=False):
        return {"url": "https://cdn.example/x", "title": "測試", "webpage_url": query}


@pytest.mark.asyncio
async def test_resolve_yt_query_prefers_browser_cookies_when_enabled():
    cog = _make_cog()
    captured = {}

    def _factory(opts):
        captured.update(opts)
        return _FakeYDL(opts)

    with patch("cogs.music_cog._YT_COOKIES_FROM_BROWSER", "chrome"), \
         patch("cogs.music_cog.os.path.exists", return_value=True), \
         patch("cogs.music_cog.yt_dlp.YoutubeDL", _factory):
        await cog._resolve_yt_query("https://www.youtube.com/watch?v=abc123")

    # browser 啟用時優先用它，即使 cookiefile 也存在也不該同時帶兩種
    assert captured.get("cookiesfrombrowser") == ("chrome",)
    assert "cookiefile" not in captured
    assert captured.get("remote_components") == ["ejs:github"]


@pytest.mark.asyncio
async def test_resolve_yt_query_uses_cookiefile_when_browser_disabled():
    cog = _make_cog()
    captured = {}

    def _factory(opts):
        captured.update(opts)
        return _FakeYDL(opts)

    with patch("cogs.music_cog._YT_COOKIES_FROM_BROWSER", None), \
         patch("cogs.music_cog.os.path.exists", return_value=True), \
         patch("cogs.music_cog.yt_dlp.YoutubeDL", _factory):
        await cog._resolve_yt_query("https://www.youtube.com/watch?v=abc123")

    assert captured.get("cookiefile") == _cookies_path()
    assert captured.get("remote_components") == ["ejs:github"]


@pytest.mark.asyncio
async def test_resolve_yt_query_skips_cookies_when_none_available():
    """browser 停用、cookiefile 不存在 → 完全不帶 cookies 相關 opts，退回原本
    匿名解析，零行為改變。"""
    cog = _make_cog()
    captured = {}

    def _factory(opts):
        captured.update(opts)
        return _FakeYDL(opts)

    with patch("cogs.music_cog._YT_COOKIES_FROM_BROWSER", None), \
         patch("cogs.music_cog.os.path.exists", return_value=False), \
         patch("cogs.music_cog.yt_dlp.YoutubeDL", _factory):
        await cog._resolve_yt_query("https://www.youtube.com/watch?v=abc123")

    assert "cookiesfrombrowser" not in captured
    assert "cookiefile" not in captured
    assert "remote_components" not in captured
