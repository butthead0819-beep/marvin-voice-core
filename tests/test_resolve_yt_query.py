"""
TDD：_resolve_yt_query 行為 — 只用 ytsearch5。

歷史：5/17 曾嘗試先打 ytmsearch5: → fallback ytsearch5: 想解 Bug 2「冷門歌
YT Music 找得到但 ytsearch 沒」，但 yt-dlp 2026.03.17 沒有 ytmsearch:
extractor，每次 ytmsearch5: 都拋 NoSupportingHandlers 在 thread executor 內
觸發 lock 競爭產生 Errno 11 deadlock（5/18 多次點歌失敗 incident）。

撤回到單純 ytsearch5:。Bug 2 另外規劃（可能走 music.youtube.com URL 形式
或 youtube:music:search_url extractor）。
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
    bot.music_memory = None
    from cogs.music_cog import MusicCog
    return MusicCog(bot)


class _FakeYDL:
    """Mock yt_dlp.YoutubeDL — 紀錄被打過的搜尋字串，按腳本回 entries。"""

    def __init__(self, ydl_opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

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
        "webpage_url": "https://youtube.com/watch?v=xxx",
        "thumbnail": "t.png",
    }


@pytest.mark.asyncio
async def test_resolve_yt_query_uses_ytsearch5_only():
    """搜尋時只打 ytsearch5:，不再 fallback ytmsearch5（yt-dlp 不支援）。"""
    cog = _make_cog()
    _FakeYDL.calls = []
    _FakeYDL.script = {
        "ytsearch5:": [_music_entry("YT Hit")],
    }

    with patch("yt_dlp.YoutubeDL", _FakeYDL):
        info = await cog._resolve_yt_query("陶喆 普通朋友")

    assert info is not None
    assert info["title"] == "YT Hit"
    assert len(_FakeYDL.calls) == 1, f"應只打一次 yt-dlp，實際 {len(_FakeYDL.calls)} 次"
    assert _FakeYDL.calls[0].startswith("ytsearch5:"), \
        f"必須是 ytsearch5:，實際是 {_FakeYDL.calls[0]}"


@pytest.mark.asyncio
async def test_resolve_yt_query_does_not_call_ytmsearch5():
    """regression: 不該再嘗試 ytmsearch5: (yt-dlp 不支援會拋 NoSupportingHandlers)."""
    cog = _make_cog()
    _FakeYDL.calls = []
    _FakeYDL.script = {"ytsearch5:": [_music_entry("X")]}

    with patch("yt_dlp.YoutubeDL", _FakeYDL):
        await cog._resolve_yt_query("任意關鍵字")

    for call in _FakeYDL.calls:
        assert not call.startswith("ytmsearch"), \
            f"不該打 ytmsearch（yt-dlp 不支援），實際呼叫: {_FakeYDL.calls}"


@pytest.mark.asyncio
async def test_resolve_yt_query_returns_none_when_empty():
    """ytsearch5: 0 命中時，回 None 讓上層回報找不到。"""
    cog = _make_cog()
    _FakeYDL.calls = []
    _FakeYDL.script = {"ytsearch5:": []}

    with patch("yt_dlp.YoutubeDL", _FakeYDL):
        info = await cog._resolve_yt_query("zzz random nonsense xyz123")

    assert info is None


@pytest.mark.asyncio
async def test_force_fresh_bypasses_resolve_cache():
    """403 重試：force_fresh=True 要跳過 videoId 快取、重抓新串流 URL。

    bug（2026-07-07 17:01）：歌播 0.7s 403 → 重試 _resolve_yt_query(webpage_url)
    卻命中 _yt_resolve_cache 拿回同一份死 URL → 又 403 → 歌被自動推薦洗掉。
    """
    import time as _t
    cog = _make_cog()
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    # 預塞「死 URL」快取（模擬剛 403 那份）
    cog._yt_resolve_cache.put(
        "dQw4w9WgXcQ", {"title": "T", "url": "http://dead.403/x", "webpage_url": url}, _t.time()
    )

    class _FreshYDL(_FakeYDL):
        def extract_info(self, search, download=False):
            type(self).calls.append(search)
            return {"title": "T", "url": "http://fresh.ok/new", "uploader": "C",
                    "categories": ["Music"], "duration": 200,
                    "webpage_url": url, "thumbnail": "t.png"}

    # 預設：命中快取死 URL、不打 yt-dlp
    _FreshYDL.calls = []
    with patch("yt_dlp.YoutubeDL", _FreshYDL):
        cached = await cog._resolve_yt_query(url)
    assert cached["url"] == "http://dead.403/x"
    assert _FreshYDL.calls == []

    # force_fresh：跳過快取、真的重抓新 URL
    _FreshYDL.calls = []
    with patch("yt_dlp.YoutubeDL", _FreshYDL):
        fresh = await cog._resolve_yt_query(url, force_fresh=True)
    assert fresh["url"] == "http://fresh.ok/new"
    assert len(_FreshYDL.calls) == 1


@pytest.mark.asyncio
async def test_resolve_yt_query_url_uses_direct_extract():
    """URL 查詢不該加 ytsearch 前綴，要直接 extract_info(url)。"""
    cog = _make_cog()
    _FakeYDL.calls = []

    class _DirectYDL(_FakeYDL):
        def extract_info(self, search, download=False):
            type(self).calls.append(search)
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
        f"URL 應該直接打，不該加搜尋前綴：{_DirectYDL.calls}"


@pytest.mark.asyncio
async def test_itunes_cover_query_uses_original_text_not_dirty_yt_title():
    """文字查詢時，iTunes 封面查詢該用使用者原始文字（乾淨），不是 yt-dlp 解析回的
    YT 標題（可能夾雜頻道名/Official MV 等上傳者自由格式雜訊）。"""
    cog = _make_cog()
    _FakeYDL.calls = []
    _FakeYDL.script = {
        "ytsearch5:": [_music_entry("周杰倫 - 晴天 (Official MV) 官方頻道")],
    }
    captured = {}

    async def _fake_resolve_metadata(title, artist=None, **kw):
        captured["title"] = title
        captured["artist"] = artist
        return None

    with patch("yt_dlp.YoutubeDL", _FakeYDL), \
         patch("itunes_cover.resolve_metadata", _fake_resolve_metadata):
        await cog._resolve_yt_query("周杰倫 晴天")

    assert captured["title"] == "周杰倫 晴天", \
        f"應該用原始查詢文字查 iTunes，實際傳入：{captured}"


@pytest.mark.asyncio
async def test_itunes_cover_falls_back_to_yt_title_when_no_orig_query():
    """直接點 URL（沒有對應文字查詢）時，維持舊行為：用 YT 標題 + uploader 查。"""
    cog = _make_cog()
    _FakeYDL.calls = []

    class _DirectYDL(_FakeYDL):
        def extract_info(self, search, download=False):
            type(self).calls.append(search)
            return {
                "title": "Direct URL Song",
                "url": "http://stream.direct/x",
                "uploader": "Channel",
                "categories": ["Music"],
                "duration": 200,
                "webpage_url": search,
                "thumbnail": "t.png",
            }

    captured = {}

    async def _fake_resolve_metadata(title, artist=None, **kw):
        captured["title"] = title
        captured["artist"] = artist
        return None

    with patch("yt_dlp.YoutubeDL", _DirectYDL), \
         patch("itunes_cover.resolve_metadata", _fake_resolve_metadata):
        await cog._resolve_yt_query("https://youtu.be/abc123")

    assert captured["title"] == "Direct URL Song"
    assert captured["artist"] == "Channel"


@pytest.mark.asyncio
async def test_itunes_metadata_writes_artist_and_album_into_result():
    """2026-08-21：iTunes 配到 artist/album 時要真的寫進結果 dict——這是
    /car_now → AVRCP 車機顯示演出者/專輯的資料源頭（見 music_cog.py::_apply_itunes_cover）。"""
    cog = _make_cog()
    _FakeYDL.calls = []
    _FakeYDL.script = {
        "ytsearch5:": [_music_entry("周杰倫 - 晴天 (Official MV) 官方頻道")],
    }

    async def _fake_resolve_metadata(title, artist=None, **kw):
        return {"cover": "https://itunes/cover.jpg", "artist": "周杰倫", "album": "范特西"}

    with patch("yt_dlp.YoutubeDL", _FakeYDL), \
         patch("itunes_cover.resolve_metadata", _fake_resolve_metadata):
        res = await cog._resolve_yt_query("周杰倫 晴天")

    assert res["thumbnail"] == "https://itunes/cover.jpg"
    assert res["artist"] == "周杰倫"
    assert res["album"] == "范特西"


@pytest.mark.asyncio
async def test_itunes_metadata_none_leaves_artist_and_album_unset():
    """iTunes 配不到（None）→ 不寫入 res['artist']/res['album']，呼叫端（/car_now）
    退回 yt-dlp 的 uploader/空字串，不是硬塞一個 None 進去蓋掉。"""
    cog = _make_cog()
    _FakeYDL.calls = []
    _FakeYDL.script = {
        "ytsearch5:": [_music_entry("周杰倫 - 晴天 (Official MV) 官方頻道")],
    }

    async def _fake_resolve_metadata(title, artist=None, **kw):
        return None

    with patch("yt_dlp.YoutubeDL", _FakeYDL), \
         patch("itunes_cover.resolve_metadata", _fake_resolve_metadata):
        res = await cog._resolve_yt_query("周杰倫 晴天")

    assert "artist" not in res
    assert "album" not in res
