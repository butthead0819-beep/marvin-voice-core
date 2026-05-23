"""
tests/run_track_quality_standalone.py — Standalone test runner

繞過 pytest（這個 environment pytest collect hang），直接呼測試 fn 並 assert。
正常 ship 後改回 pytest；這個檔只在環境 broken 時用。

Run: venv_simon/bin/python tests/run_track_quality_standalone.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import traceback
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# 確保 import track_quality from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import track_quality as tq


PASSED = 0
FAILED = 0
FAILURES = []


def run(name, fn):
    global PASSED, FAILED
    try:
        if asyncio.iscoroutinefunction(fn):
            asyncio.run(fn())
        else:
            fn()
        print(f"  ✓ {name}")
        PASSED += 1
    except AssertionError as e:
        print(f"  ✗ {name}: {e}")
        FAILURES.append((name, traceback.format_exc()))
        FAILED += 1
    except Exception as e:
        print(f"  ✗ {name} ERROR: {type(e).__name__}: {e}")
        FAILURES.append((name, traceback.format_exc()))
        FAILED += 1


# ── URL parsing tests ────────────────────────────────────────────────────────

def t_extract_youtube_com():
    assert tq.extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

def t_extract_youtu_be():
    assert tq.extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

def t_extract_short_with_params():
    assert tq.extract_video_id("https://youtu.be/dQw4w9WgXcQ?t=42") == "dQw4w9WgXcQ"

def t_extract_non_youtube_none():
    assert tq.extract_video_id("https://example.com/foo") is None


# ── Cover heuristic ──────────────────────────────────────────────────────────

def t_cover_positives():
    titles = [
        "晴天 (cover by 某某)",
        "周杰倫 - 晴天 翻唱",
        "Yesterday Acoustic Version",
        "演員 cover",
        "Bohemian Rhapsody (acoustic cover)",
    ]
    for title in titles:
        assert tq.looks_like_cover(title) is True, f"should be cover: {title}"

def t_cover_negatives():
    titles = [
        "周杰倫 - 晴天 (Official MV)",
        "Bohemian Rhapsody - Official Audio",
        "Stay With Me - Sam Smith [Official Music Video]",
        "晴天 周杰倫",
        "演員 - 薛之謙",
    ]
    for title in titles:
        assert tq.looks_like_cover(title) is False, f"should NOT be cover: {title}"

# NOTE: 「official cover」混合 marker 是 ambiguous edge case，現實 YouTube 罕見
# 且歧義（cover album 本來就是 cover）。v1 不擔保此 edge case 的判定。


# ── CoverBlacklist ───────────────────────────────────────────────────────────

def t_blacklist_empty():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        bl = tq.CoverBlacklist(path=f"{td}/bl.json")
        assert bl.is_blacklisted("https://youtu.be/abc") is False

def t_blacklist_add_check():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        bl = tq.CoverBlacklist(path=f"{td}/bl.json")
        bl.add("https://youtu.be/abc", reason="bad cover")
        assert bl.is_blacklisted("https://youtu.be/abc") is True

def t_blacklist_normalizes_video_id():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        bl = tq.CoverBlacklist(path=f"{td}/bl.json")
        bl.add("https://www.youtube.com/watch?v=abc123XYZ_-", reason="test")
        assert bl.is_blacklisted("https://youtu.be/abc123XYZ_-") is True

def t_blacklist_persists():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = f"{td}/bl.json"
        bl1 = tq.CoverBlacklist(path=p)
        bl1.add("https://youtu.be/abc", reason="test")
        bl2 = tq.CoverBlacklist(path=p)
        bl2.load()
        assert bl2.is_blacklisted("https://youtu.be/abc") is True

def t_blacklist_missing_file_empty():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        bl = tq.CoverBlacklist(path=f"{td}/nope.json")
        bl.load()
        assert bl.is_blacklisted("https://youtu.be/abc") is False


# ── assess_track_quality (integration with mock) ─────────────────────────────

async def t_high_play_cover_passes():
    with patch.object(tq, "fetch_video_view_count", new=AsyncMock(return_value=1_000_000)):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            bl = tq.CoverBlacklist(path=f"{td}/bl.json")
            passes, reason = await tq.assess_track_quality(
                "https://youtu.be/abc", "晴天 (cover by 某某)",
                api_key="fake", blacklist=bl,
            )
            assert passes is True, f"expect pass got {passes} reason={reason}"
            assert reason == "ok", f"expect ok got {reason}"

async def t_low_play_cover_blocked():
    with patch.object(tq, "fetch_video_view_count", new=AsyncMock(return_value=50_000)):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            bl = tq.CoverBlacklist(path=f"{td}/bl.json")
            passes, reason = await tq.assess_track_quality(
                "https://youtu.be/abc", "晴天 (cover)",
                api_key="fake", blacklist=bl,
            )
            assert passes is False, f"expect block got {passes}"
            assert reason == "low_views_cover", f"expect low_views_cover got {reason}"

async def t_low_play_non_cover_still_passes():
    with patch.object(tq, "fetch_video_view_count", new=AsyncMock(return_value=10_000)):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            bl = tq.CoverBlacklist(path=f"{td}/bl.json")
            passes, reason = await tq.assess_track_quality(
                "https://youtu.be/abc", "週末暢談 (Official Audio)",
                api_key="fake", blacklist=bl,
            )
            assert passes is True, f"expect pass got {passes} reason={reason}"
            assert reason == "ok"

async def t_blacklist_hit_blocks():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        bl = tq.CoverBlacklist(path=f"{td}/bl.json")
        bl.add("https://youtu.be/abc", reason="manually banned")
        with patch.object(tq, "fetch_video_view_count", new=AsyncMock(return_value=1_000_000)):
            passes, reason = await tq.assess_track_quality(
                "https://youtu.be/abc", "anything",
                api_key="fake", blacklist=bl,
            )
            assert passes is False, "blacklist hit should block"
            assert reason == "blacklisted", f"expect blacklisted got {reason}"

async def t_api_error_fail_open():
    async def _broken(*args, **kwargs):
        raise tq.YouTubeAPIError("quota exceeded")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        bl = tq.CoverBlacklist(path=f"{td}/bl.json")
        with patch.object(tq, "fetch_video_view_count", new=_broken):
            passes, reason = await tq.assess_track_quality(
                "https://youtu.be/abc", "晴天 (cover)",
                api_key="fake", blacklist=bl,
            )
            assert passes is True, "API error should fail-open"
            assert reason == "api_error_fail_open", f"expect api_error got {reason}"

async def t_invalid_url_fail_open():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        bl = tq.CoverBlacklist(path=f"{td}/bl.json")
        with patch.object(tq, "fetch_video_view_count", new=AsyncMock(return_value=1_000_000)):
            passes, reason = await tq.assess_track_quality(
                "https://example.com/not-youtube", "whatever (cover)",  # 需是 cover 才會進 video_id 檢查
                api_key="fake", blacklist=bl,
            )
            assert passes is True
            assert reason == "invalid_url_fail_open"


# ── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== M1 track_quality.py standalone tests ===")
    print()
    print("URL parsing:")
    run("extract_video_id youtube.com", t_extract_youtube_com)
    run("extract_video_id youtu.be", t_extract_youtu_be)
    run("extract_video_id youtu.be with params", t_extract_short_with_params)
    run("extract_video_id non-youtube → None", t_extract_non_youtube_none)
    print()
    print("Cover heuristic:")
    run("cover positives (5 titles)", t_cover_positives)
    run("cover negatives (5 titles)", t_cover_negatives)
    print()
    print("CoverBlacklist:")
    run("empty blacklist", t_blacklist_empty)
    run("add then check", t_blacklist_add_check)
    run("normalizes video_id", t_blacklist_normalizes_video_id)
    run("persists to file", t_blacklist_persists)
    run("missing file → empty", t_blacklist_missing_file_empty)
    print()
    print("assess_track_quality (with mock):")
    run("high play cover passes", t_high_play_cover_passes)
    run("low play cover blocked", t_low_play_cover_blocked)
    run("low play non-cover still passes", t_low_play_non_cover_still_passes)
    run("blacklist hit blocks", t_blacklist_hit_blocks)
    run("API error → fail-open", t_api_error_fail_open)
    run("invalid URL → fail-open", t_invalid_url_fail_open)

    print()
    print(f"=== Results: {PASSED} passed, {FAILED} failed ===")
    if FAILED:
        print("\n--- Failures ---")
        for name, tb in FAILURES:
            print(f"\n{name}:")
            print(tb)
        sys.exit(1)
