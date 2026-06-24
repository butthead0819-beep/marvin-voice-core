"""
tests/test_track_quality.py — Cover Quality Hard Filter 測試

對應 Phase 1 M1 (design doc Day 1-3)。

測試覆蓋：
  - video_id 從不同 URL format 解析
  - Cover heuristic（標題模式）
  - Play count threshold 對 cover vs 原版的差別處理
  - Blacklist hit / add / persist
  - YouTube API error → fail-open（per Phase 1 Failure Modes 表）
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import track_quality as tq


# ── video_id 解析 ─────────────────────────────────────────────────────────────

def test_extract_video_id_from_youtube_com():
    assert tq.extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_extract_video_id_from_youtu_be():
    assert tq.extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_extract_video_id_from_short_form_with_params():
    assert tq.extract_video_id("https://youtu.be/dQw4w9WgXcQ?t=42") == "dQw4w9WgXcQ"


def test_extract_video_id_returns_none_for_non_youtube():
    assert tq.extract_video_id("https://example.com/foo") is None


# ── Cover heuristic ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("title", [
    "晴天 (cover by 某某)",
    "周杰倫 - 晴天 翻唱",
    "Yesterday Acoustic Version",
    "演員 cover",
    "Bohemian Rhapsody (acoustic cover)",
])
def test_looks_like_cover_positive(title):
    assert tq.looks_like_cover(title) is True


@pytest.mark.parametrize("title", [
    "周杰倫 - 晴天 (Official MV)",
    "Bohemian Rhapsody - Official Audio",
    "Stay With Me - Sam Smith [Official Music Video]",
    "晴天 周杰倫",
    "演員 - 薛之謙",
])
def test_looks_like_cover_negative(title):
    assert tq.looks_like_cover(title) is False


@pytest.mark.parametrize("title", [
    "Eagles - Hotel California (Live 1977) (Official Video) [HD]",
    "陳昇【鼓聲若響】'95美麗的寶島演唱會 Bobby Chen New Year Live '95 Concert",
    "張惠妹 - 聽海 Live Concert",
    "五月天 - 倔強 (不插電版)",
    "Coldplay - Yellow (Live Version)",
])
def test_looks_like_live_positive(title):
    assert tq.looks_like_live(title) is True


@pytest.mark.parametrize("title", [
    "周杰倫 - 晴天 (Official MV)",
    "Paul McCartney - Live and Let Die",          # 「live」是歌名一部分，非現場版
    "Daft Punk - Alive",
    "晴天 周杰倫",
])
def test_looks_like_live_negative(title):
    assert tq.looks_like_live(title) is False


# NOTE: 「official cover」混合 marker 是 ambiguous edge case（cover album 本來
# 就是 cover）。v1 不擔保此 edge case 判定，pytest 不寫對應 case。


# ── CoverBlacklist ────────────────────────────────────────────────────────────

@pytest.fixture
def temp_blacklist(tmp_path):
    return tq.CoverBlacklist(path=str(tmp_path / "blacklist.json"))


def test_blacklist_empty_initially(temp_blacklist):
    assert temp_blacklist.is_blacklisted("https://youtu.be/abc") is False


def test_blacklist_add_then_check(temp_blacklist):
    temp_blacklist.add("https://youtu.be/abc", reason="bad cover")
    assert temp_blacklist.is_blacklisted("https://youtu.be/abc") is True


def test_blacklist_normalizes_to_video_id(temp_blacklist):
    """同一 video_id 不同 URL format 應該都命中。"""
    temp_blacklist.add("https://www.youtube.com/watch?v=abc123XYZ_-", reason="test")
    assert temp_blacklist.is_blacklisted("https://youtu.be/abc123XYZ_-") is True


def test_blacklist_persists_to_file(tmp_path):
    p = tmp_path / "bl.json"
    bl1 = tq.CoverBlacklist(path=str(p))
    bl1.add("https://youtu.be/abc", reason="test")
    bl1.save()

    bl2 = tq.CoverBlacklist(path=str(p))
    bl2.load()
    assert bl2.is_blacklisted("https://youtu.be/abc") is True


def test_blacklist_missing_file_is_empty(tmp_path):
    bl = tq.CoverBlacklist(path=str(tmp_path / "nope.json"))
    bl.load()
    assert bl.is_blacklisted("https://youtu.be/abc") is False


# ── assess_track_quality (integration) ────────────────────────────────────────

@pytest.fixture
def mock_fetch_views():
    """Patch fetch_video_view_count, default 高播放數。"""
    with patch.object(tq, "fetch_video_view_count", new=AsyncMock(return_value=1_000_000)) as m:
        yield m


@pytest.mark.asyncio
async def test_high_play_cover_passes(mock_fetch_views, temp_blacklist):
    passes, reason = await tq.assess_track_quality(
        "https://youtu.be/abc", "晴天 (cover by 某某)",
        api_key="fake", blacklist=temp_blacklist,
    )
    assert passes is True
    assert reason == "ok"


@pytest.mark.asyncio
async def test_low_play_cover_blocked(mock_fetch_views, temp_blacklist):
    mock_fetch_views.return_value = 50_000  # 遠低於 500k threshold
    passes, reason = await tq.assess_track_quality(
        "https://youtu.be/abc", "晴天 (cover)",
        api_key="fake", blacklist=temp_blacklist,
    )
    assert passes is False
    assert reason == "low_views_cover"


@pytest.mark.asyncio
async def test_low_play_non_cover_still_passes(mock_fetch_views, temp_blacklist):
    """低播放原版（niche 好歌）不該被擋——只擋低播放 cover。"""
    mock_fetch_views.return_value = 10_000
    passes, reason = await tq.assess_track_quality(
        "https://youtu.be/abc", "週末暢談 (Official Audio)",
        api_key="fake", blacklist=temp_blacklist,
    )
    assert passes is True
    assert reason == "ok"


@pytest.mark.asyncio
async def test_blacklist_hit_blocks(mock_fetch_views, temp_blacklist):
    temp_blacklist.add("https://youtu.be/abc", reason="manually banned")
    passes, reason = await tq.assess_track_quality(
        "https://youtu.be/abc", "anything",
        api_key="fake", blacklist=temp_blacklist,
    )
    assert passes is False
    assert reason == "blacklisted"


@pytest.mark.asyncio
async def test_api_error_fail_open(monkeypatch, temp_blacklist):
    """YouTube API 失敗 → fail-open，per Phase 1 Failure Modes 表。"""
    async def _broken(*args, **kwargs):
        raise tq.YouTubeAPIError("quota exceeded")
    monkeypatch.setattr(tq, "fetch_video_view_count", _broken)

    passes, reason = await tq.assess_track_quality(
        "https://youtu.be/abc", "晴天 (cover)",
        api_key="fake", blacklist=temp_blacklist,
    )
    assert passes is True
    assert reason == "api_error_fail_open"


@pytest.mark.asyncio
async def test_invalid_url_fail_open(mock_fetch_views, temp_blacklist):
    """無法解析 video_id → fail-open。

    title 須 looks_like_cover 才會走到 video_id 解析分支
    （非 cover 在更前面就 (True, "ok") 早退）。
    """
    passes, reason = await tq.assess_track_quality(
        "https://example.com/not-youtube", "whatever (cover)",
        api_key="fake", blacklist=temp_blacklist,
    )
    assert passes is True
    assert reason == "invalid_url_fail_open"
