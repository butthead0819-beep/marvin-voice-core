"""
track_quality.py — Cover Quality Hard Filter

Phase 1 M1。在 autopilot 把 YouTube info 放進 stream_queue 前過一道：
  - 低播放 cover 直接 ban（避免「乾脆只聽原版」災難）
  - 原版即使 niche play count 仍放行（不錯殺）
  - 黑名單 hit 直接 ban
  - API 失敗 fail-open（不阻塞 autopilot，per design doc Phase 1 Failure Modes 表）

Caller pattern (in voice_controller autopilot loop):
    bl = CoverBlacklist.shared()
    passes, reason = await assess_track_quality(info['url'], info['title'], blacklist=bl)
    if not passes:
        logger.info(f"[Quality] block {info['title']}: {reason}")
        continue
    stream_queue.append(info)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

import aiohttp

logger = logging.getLogger(__name__)


# ── 常數 ─────────────────────────────────────────────────────────────────────

DEFAULT_THRESHOLD_COVER_VIEWS = 500_000   # cover 必須超過的播放數
DEFAULT_BLACKLIST_PATH = "data/bad_cover_blacklist.json"
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3/videos"
DEFAULT_API_TIMEOUT_S = 5.0

# Cover 關鍵字 (case-insensitive)
_COVER_KEYWORDS = [
    "cover",
    "翻唱",
    "acoustic version",
    "acoustic cover",
    "remake",
    "covered by",
]
# 抵消 cover keyword 的 official marker
_OFFICIAL_MARKERS = [
    "official mv",
    "official music video",
    "official audio",
    "official video",
    "(official)",
    "[official]",
    "官方",
]


# ── Exceptions ───────────────────────────────────────────────────────────────

class YouTubeAPIError(Exception):
    pass


# ── URL parsing ──────────────────────────────────────────────────────────────

def extract_video_id(url: str) -> Optional[str]:
    """從各種 YouTube URL format 抽出 video_id；非 YouTube → None。"""
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    if host in ("youtu.be",):
        # https://youtu.be/<ID>[?params]
        vid = parsed.path.lstrip("/")
        return vid or None
    if host in ("www.youtube.com", "youtube.com", "m.youtube.com", "music.youtube.com"):
        # https://www.youtube.com/watch?v=<ID>
        qs = parse_qs(parsed.query)
        if "v" in qs and qs["v"]:
            return qs["v"][0]
        # /embed/<ID>, /shorts/<ID>
        for prefix in ("/embed/", "/shorts/", "/v/"):
            if parsed.path.startswith(prefix):
                return parsed.path[len(prefix):].split("/")[0] or None
    return None


# ── Cover heuristic ──────────────────────────────────────────────────────────

def looks_like_cover(title: str) -> bool:
    """
    True = 標題暗示這是 cover 版本。
    Official marker 會壓制 cover 判定（原版即使含 "cover" 字眼也不算）。
    """
    if not title:
        return False
    lower = title.lower()
    has_official = any(m in lower for m in _OFFICIAL_MARKERS)
    if has_official:
        return False
    has_cover_kw = any(kw in lower for kw in _COVER_KEYWORDS)
    return has_cover_kw


# ── YouTube Data API ─────────────────────────────────────────────────────────

async def fetch_video_view_count(
    video_id: str,
    api_key: str,
    *,
    timeout_s: float = DEFAULT_API_TIMEOUT_S,
) -> int:
    """呼叫 YouTube Data API v3 取 view_count。失敗 raise YouTubeAPIError。"""
    params = {
        "id": video_id,
        "key": api_key,
        "part": "statistics",
    }
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                YOUTUBE_API_BASE,
                params=params,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as resp:
                if resp.status == 403:
                    raise YouTubeAPIError(f"403 (likely quota exceeded): {await resp.text()}")
                if resp.status != 200:
                    raise YouTubeAPIError(f"HTTP {resp.status}: {await resp.text()}")
                data = await resp.json()
    except aiohttp.ClientError as e:
        raise YouTubeAPIError(f"network: {e}") from e
    except asyncio.TimeoutError as e:
        raise YouTubeAPIError(f"timeout {timeout_s}s") from e

    items = data.get("items", [])
    if not items:
        raise YouTubeAPIError(f"empty result for video_id={video_id}")
    stats = items[0].get("statistics", {})
    try:
        return int(stats.get("viewCount", 0))
    except (TypeError, ValueError) as e:
        raise YouTubeAPIError(f"bad viewCount: {stats}") from e


# ── CoverBlacklist ───────────────────────────────────────────────────────────

class CoverBlacklist:
    """簡單 video_id → {reason, added_ts} 黑名單，自動 persist。"""

    _shared_instance: Optional["CoverBlacklist"] = None

    def __init__(self, path: str = DEFAULT_BLACKLIST_PATH):
        self._path = Path(path)
        self._data: dict[str, dict] = {}
        self.load()

    @classmethod
    def shared(cls, path: str = DEFAULT_BLACKLIST_PATH) -> "CoverBlacklist":
        """取共用實例（簡單 lazy singleton）。"""
        if cls._shared_instance is None or str(cls._shared_instance._path) != path:
            cls._shared_instance = cls(path=path)
        return cls._shared_instance

    def is_blacklisted(self, url_or_id: str) -> bool:
        vid = extract_video_id(url_or_id) or url_or_id
        return vid in self._data

    def add(self, url_or_id: str, reason: str) -> None:
        import time
        vid = extract_video_id(url_or_id) or url_or_id
        if not vid:
            return
        self._data[vid] = {"reason": reason, "added_ts": time.time()}
        try:
            self.save()
        except Exception:
            logger.exception("[CoverBlacklist] save failed")

    def load(self) -> None:
        if not self._path.exists():
            self._data = {}
            return
        try:
            with self._path.open("r", encoding="utf-8") as f:
                self._data = json.load(f)
        except Exception:
            logger.exception("[CoverBlacklist] load failed, starting empty")
            self._data = {}

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)


# ── Main API ─────────────────────────────────────────────────────────────────

async def assess_track_quality(
    yt_url: str,
    yt_title: str,
    *,
    api_key: Optional[str] = None,
    threshold_views: int = DEFAULT_THRESHOLD_COVER_VIEWS,
    blacklist: Optional[CoverBlacklist] = None,
) -> tuple[bool, str]:
    """
    Returns (passes, reason).
      passes=True  → 該歌可放進 stream_queue
      reason 值:
        - "ok"                    → 通過
        - "blacklisted"           → 黑名單命中
        - "low_views_cover"       → 是 cover 且 play_count < threshold
        - "invalid_url_fail_open" → URL 無法解析 video_id（fail-open）
        - "api_error_fail_open"   → YouTube API 失敗（fail-open）
    """
    # API key 預設用 env (Jack 把 YouTube Data API 加進 Gemini paid project)
    if api_key is None:
        api_key = os.getenv("GEMINI_PAID_API_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("YOUTUBE_API_KEY")

    # 黑名單先擋
    if blacklist is not None and blacklist.is_blacklisted(yt_url):
        return (False, "blacklisted")

    # 非 cover → 不擋（即使低播放原版也放行）
    if not looks_like_cover(yt_title):
        return (True, "ok")

    # 是 cover → 拿 view count 比 threshold
    video_id = extract_video_id(yt_url)
    if not video_id:
        return (True, "invalid_url_fail_open")
    if not api_key:
        logger.warning("[track_quality] no API key, fail-open")
        return (True, "api_error_fail_open")
    try:
        views = await fetch_video_view_count(video_id, api_key)
    except YouTubeAPIError as e:
        logger.warning(f"[track_quality] API error for {video_id}: {e} — fail-open")
        return (True, "api_error_fail_open")

    if views < threshold_views:
        return (False, "low_views_cover")
    return (True, "ok")
