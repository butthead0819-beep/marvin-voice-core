"""Spotify Search 乾淨曲目 metadata 解析（純非同步，可注入 fetch）。

Why：YouTube 標題髒（Official MV/畫質/廠牌雜訊），新歌一入庫就該是乾淨的，別
靠事後批次清洗（見 scripts/spotify_clean_music_memory.py，只清存量）。掛在跟
itunes_cover.py 同一個 resolve 掛點（music_cog.py::_apply_itunes_cover 旁）。

用 Spotify Search API 的 **Client Credentials flow**（App-only、免使用者登入）——
跟 [[project_spotify_connect_personal_dj_design]] 的個人 OAuth（Connect 控制播放）
是兩條獨立的路，metadata 查詢本來就不需要動到播放權限，`.env` 裡
`SPOTIFY_CLIENT_ID/SECRET` 原本的註解「只用來讀公開歌單 metadata」就是這個用途。

策略沿用 spotify_query_build.py 的 field-scoped 查詢（track:/artist: 精準一個
量級）+ 自由文字備援，跟 itunes_cover.py 一樣的 fail-safe 護欄：
  • 失敗/逾時/空結果/關 flag（MARVIN_SPOTIFY_METADATA=0）一律回 None，絕不擋播放。
  • field 查詢命中後仍比一次相似度守門（擋 track: candidate 太短/太雜誤配到完全
    不相關曲目——scripts/spotify_clean_music_memory.py 實測撞過巴哈管弦組曲）。
"""
from __future__ import annotations

import asyncio
import base64
import os
import time
from typing import Awaitable, Callable, Optional

try:
    import aiohttp
except Exception:  # pragma: no cover - aiohttp 缺席時走 fallback
    aiohttp = None

from song_name_clean import clean_title_regex
from itunes_cover import _clean_artist, _norm, _similarity
from spotify_query_build import build_field_queries

TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"
FIELD_SANITY_THRESHOLD = 0.3
FREE_TEXT_THRESHOLD = 0.55

_token_cache: dict[str, object] = {"token": None, "expires_at": 0.0}


def enabled() -> bool:
    return os.getenv("MARVIN_SPOTIFY_METADATA", "1").lower() not in ("0", "false", "no", "")


async def _get_token(*, timeout_s: float = 6.0) -> Optional[str]:
    if _token_cache["token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["token"]  # type: ignore[return-value]

    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret or aiohttp is None:
        return None

    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                headers={"Authorization": f"Basic {auth}"},
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, Exception):
        return None

    token = data.get("access_token")
    if not token:
        return None
    _token_cache["token"] = token
    _token_cache["expires_at"] = time.time() + max(60, int(data.get("expires_in", 3600)) - 60)
    return token


async def _default_fetch(query: str, *, timeout_s: float = 6.0) -> Optional[dict]:
    if aiohttp is None:
        return None
    token = await _get_token(timeout_s=timeout_s)
    if not token:
        return None
    params = {"q": query, "type": "track", "limit": 5}
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                SEARCH_URL,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, Exception):
        return None


def _track_from_response(data: Optional[dict]) -> Optional[dict]:
    if not data:
        return None
    items = (data.get("tracks") or {}).get("items") or []
    return items[0] if items else None


async def _resolve_best(
    title: str,
    artist: Optional[str] = None,
    *,
    fetch: Optional[Callable[..., Awaitable[Optional[dict]]]] = None,
) -> Optional[dict]:
    if not enabled() or not title:
        return None

    _fetch = fetch or _default_fetch
    cleaned = clean_title_regex(title) or title
    artist_c = _clean_artist(artist) or ""

    for q, track_candidate in build_field_queries(cleaned, artist_c):
        data = await _fetch(q)
        track = _track_from_response(data)
        if track and _similarity(track_candidate, track["name"]) >= FIELD_SANITY_THRESHOLD:
            return track

    query = f"{artist_c} {cleaned}".strip() if artist_c else cleaned
    data = await _fetch(query)
    items = (data.get("tracks") or {}).get("items") or [] if data else []
    if not items:
        return None

    ncleaned = _norm(cleaned)
    best_score, best = 0.0, None
    for t in items:
        t_artist = ", ".join(a["name"] for a in t["artists"])
        cand = f"{t_artist} {t['name']}".strip()
        score = max(_similarity(query, cand), _similarity(cleaned, t["name"]))
        ntrack = _norm(t["name"])
        if ntrack and (ncleaned in ntrack or ntrack in ncleaned):
            score = max(score, 0.7)
        if score > best_score:
            best_score, best = score, t

    return best if best_score >= FREE_TEXT_THRESHOLD else None


async def resolve_metadata(
    title: str,
    artist: Optional[str] = None,
    *,
    fetch: Optional[Callable[..., Awaitable[Optional[dict]]]] = None,
) -> Optional[dict]:
    """回 {"title":, "artist":, "album":, "uri":}；查不到/低信心/關閉一律回 None。"""
    best = await _resolve_best(title, artist, fetch=fetch)
    if best is None:
        return None
    return {
        "title": best.get("name") or None,
        "artist": ", ".join(a["name"] for a in best.get("artists", [])) or None,
        "album": (best.get("album") or {}).get("name") or None,
        "uri": best.get("uri") or None,
    }
