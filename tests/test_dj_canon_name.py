"""DJ 串場（LLM ctx、保底模板、/guide_song、尾段一致性保底）唸的歌手/曲名用曲庫正規化資料。

2026-09-29 使用者：DJ interjection TTS 的歌手/曲名也用正規化後的資料。
沒有正規化資料時行為不變（dj_display_name 洗 YouTube 標題）。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.test_dj_story_context import _make_cog


def _weights(monkeypatch, **w):
    """扭蛋池權重固定（dj_topic_selector.MODE_WEIGHTS）：串場 mode 改成加權隨機後，
    斷言特定 mode 素材/路徑的測試要把權重釘住才是決定性的。"""
    import dj_topic_selector
    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", w)


def _no_quick(monkeypatch):
    """排除 quick（本地模板、不呼叫 LLM），其餘照預設權重——只在乎有走 LLM 路徑的測試用。"""
    import dj_topic_selector
    w = dict(dj_topic_selector.MODE_WEIGHTS)
    w["quick"] = 0.0
    monkeypatch.setattr(dj_topic_selector, "MODE_WEIGHTS", w)

VID = "abcdefghijk"
CANON = {"artist": "鄧麗君", "title": "月亮代表我的心", "album": None, "year": 1990, "source": "shazam+itunes"}
DIRTY = "鄧麗君 -月亮代表我的心 Teresa Teng (HD) (with lyrics sing along)"


def _info(**kw):
    d = {"title": DIRTY, "requested_by": "大肚", "uploader": "lifeisgood181",
         "webpage_url": f"https://www.youtube.com/watch?v={VID}"}
    d.update(kw)
    return d


def _store(data):
    s = MagicMock()
    s.get = MagicMock(side_effect=lambda k: data.get(k))
    return s


def test_dj_clean_name_prefers_canon(tmp_path):
    cog = _make_cog(tmp_path=tmp_path)
    assert cog._dj_clean_name(_info(_canon=CANON)) == ("月亮代表我的心", "鄧麗君")


def test_dj_clean_name_without_canon_unchanged(tmp_path):
    cog = _make_cog(tmp_path=tmp_path)
    from song_name_clean import dj_display_name
    from music_memory import extract_video_id
    info = _info()
    assert cog._dj_clean_name(info) == dj_display_name(info, extract_vid=extract_video_id)


@pytest.mark.asyncio
async def test_interjection_ctx_uses_canon_resolved_this_round(tmp_path, monkeypatch):
    """這首是這輪 prefetch 才正規化完 → 開頭「歌曲：」那行換成正規化名字。"""
    _no_quick(monkeypatch)
    cog = _make_cog(tmp_path=tmp_path)
    cog._audiophile_deps = MagicMock(return_value=(_store({}), MagicMock(), cog.bot.router))
    cog._dj_song_material = AsyncMock(return_value=(CANON, None))
    await cog._fetch_dj_interjection_raw(_info())
    ctx = cog.bot.router.generate_dynamic_system_msg.await_args.kwargs["context"]
    assert ctx.splitlines()[0] == "歌曲：鄧麗君 - 月亮代表我的心"


@pytest.mark.asyncio
async def test_interjection_ctx_uses_cached_canon_from_the_start(tmp_path, monkeypatch):
    """之前正規化過的歌：一開頭就從 canon::<videoId> 快取補上，連查詢用的名字都是乾淨的。"""
    _no_quick(monkeypatch)
    cog = _make_cog(tmp_path=tmp_path)
    cog._audiophile_deps = MagicMock(return_value=(_store({f"canon::{VID}": CANON}), MagicMock(), cog.bot.router))
    material = AsyncMock(return_value=(None, None))
    cog._dj_song_material = material
    await cog._fetch_dj_interjection_raw(_info())
    assert material.await_args.args[1:] == ("月亮代表我的心", "鄧麗君")
    ctx = cog.bot.router.generate_dynamic_system_msg.await_args.kwargs["context"]
    assert ctx.splitlines()[0] == "歌曲：鄧麗君 - 月亮代表我的心"


@pytest.mark.asyncio
async def test_fallback_announcement_uses_canon(tmp_path, monkeypatch):
    """LLM 空手 → 保底報幕也唸正規化名字。"""
    _no_quick(monkeypatch)
    cog = _make_cog(tmp_path=tmp_path)
    cog.bot.router.generate_dynamic_system_msg = AsyncMock(return_value="")
    cog._audiophile_deps = MagicMock(return_value=(_store({f"canon::{VID}": CANON}), MagicMock(), cog.bot.router))
    cog._dj_song_material = AsyncMock(return_value=(None, None))
    out = await cog._fetch_dj_interjection_raw(_info())
    assert "鄧麗君演唱的月亮代表我的心" in out["text"]
