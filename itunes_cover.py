"""iTunes Search 專輯封面解析（純非同步，可注入 fetch）。

Why：AirPlay 來源多為 YouTube、YT 縮圖又髒又非方形。用 iTunes Search（免費、
免金鑰、不綁 Apple Music 訂閱）拿乾淨的方形專輯封面取代之。

護欄：
  • 先用 song_name_clean.clean_title_regex 洗掉 Official/MV/【】 等雜訊再查。
  • **強驗證**：iTunes 回的 trackName/artistName 跟查詢相似度不夠 → 退回原縮圖，
    專擋「自信地抓到 live/翻唱/合輯封面」。
  • 失敗/逾時/空結果/關 flag（MARVIN_ITUNES_COVER=0）一律回 fallback，絕不讓卡片壞。
  • 免費 → 不需 guard/記帳。
"""
from __future__ import annotations

import asyncio
import os
import re
from difflib import SequenceMatcher
from typing import Awaitable, Callable, Optional

try:
    import aiohttp
except Exception:  # pragma: no cover - aiohttp 缺席時走 fallback
    aiohttp = None

from song_name_clean import clean_title_regex

ITUNES_URL = "https://itunes.apple.com/search"
_TOKENS = re.compile(r"[0-9a-z一-鿿぀-ヿ]+")
_ARTIST_CRUFT = re.compile(
    r"(?i)\s*[-–—]?\s*(topic|vevo|official(?:\s*(?:channel|audio|video))?|channel|"
    r"music|records?|官方(?:頻道|音樂)?|頻道)\s*$"
)


def _clean_artist(a: Optional[str]) -> Optional[str]:
    """剝掉 uploader 常見頻道 cruft（- Topic / VEVO / 官方頻道…）。清空回 None。"""
    a = (a or "").strip()
    prev = None
    while prev != a:
        prev = a
        a = _ARTIST_CRUFT.sub("", a).strip()
    return a or None


def enabled() -> bool:
    return os.getenv("MARVIN_ITUNES_COVER", "1").lower() not in ("0", "false", "no", "")


def _norm(s: str) -> str:
    return " ".join(_TOKENS.findall((s or "").lower()))


def _similarity(a: str, b: str) -> float:
    """token Jaccard 與序列比對取大者（中英混雜也穩）。"""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    ta, tb = set(na.split()), set(nb.split())
    jaccard = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    return max(jaccard, SequenceMatcher(None, na, nb).ratio())


def _hi_res(url: str, size: int = 600) -> str:
    """artworkUrl100 的 100x100bb.jpg → {size}x{size}bb.jpg 高清。"""
    if not url:
        return url
    return re.sub(r"/\d+x\d+bb\.(jpg|png)", rf"/{size}x{size}bb.\1", url)


async def _default_fetch(term: str, *, timeout_s: float = 6.0) -> Optional[dict]:
    if aiohttp is None:
        return None
    params = {"term": term, "entity": "song", "media": "music", "limit": 5}
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                ITUNES_URL, params=params, timeout=aiohttp.ClientTimeout(total=timeout_s)
            ) as resp:
                if resp.status != 200:
                    return None
                # iTunes 回 text/javascript mimetype，需 content_type=None 略過檢查
                return await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, Exception):
        return None


def _art(it: dict) -> Optional[str]:
    return it.get("artworkUrl100") or it.get("artworkUrl60") or it.get("artworkUrl30")


async def _resolve_best(
    title: str,
    artist: Optional[str] = None,
    *,
    fetch: Optional[Callable[..., Awaitable[Optional[dict]]]] = None,
    threshold: float = 0.55,
) -> Optional[dict]:
    """resolve_cover()/resolve_metadata() 共用的核心：查 iTunes、比對信心，回配到的
    那筆原始候選 dict（含 artworkUrl100/artistName/trackName/collectionName）；查不到/
    低信心一律回 None，兩個呼叫端各自決定 None 時怎麼退回。

    採用規則（避開「自信地錯」又能吃 CJK 羅馬化）：
      ① 文字夠像（中↔中）→ 採用最像的一筆。
      ② 否則若有藝人、且 iTunes 把結果鎖在同一位藝人 → 信任 iTunes #1（跨語言救援）。
      ③ 沒藝人 or 結果散落多位藝人（iTunes 沒聽懂）→ None。
    """
    if not enabled() or not title:
        return None

    cleaned = clean_title_regex(title) or title
    artist_c = _clean_artist(artist)
    term = f"{artist_c} {cleaned}".strip() if artist_c else cleaned
    data = await (fetch or _default_fetch)(term)
    if not data:
        return None

    arted = [it for it in (data.get("results") or []) if _art(it)]
    if not arted:
        return None

    query = f"{artist_c or ''} {cleaned}".strip()
    ncleaned = _norm(cleaned)
    best_score, best = 0.0, None
    for it in arted:
        cand = f"{it.get('artistName', '')} {it.get('trackName', '')}".strip()
        score = max(_similarity(query, cand), _similarity(cleaned, it.get("trackName", "")))
        ntrack = _norm(it.get("trackName", ""))
        if ntrack and (ncleaned in ntrack or ntrack in ncleaned):  # "七里香 (Live)" 含 "七里香"
            score = max(score, 0.7)
        if score > best_score:
            best_score, best = score, it

    if best_score >= threshold:
        return best

    # 跨語言救援：iTunes 對中文歌常回羅馬拼音，文字必失敗。若有藝人且前幾筆鎖定
    # 同一位藝人（代表 iTunes 聽懂了藝人）→ 信任它的排名第一。
    top_artists = {_norm(it.get("artistName", "")) for it in arted[:3]}
    if artist_c and len(top_artists) == 1 and next(iter(top_artists)):
        return arted[0]
    return None


async def resolve_cover(
    title: str,
    artist: Optional[str] = None,
    *,
    fallback: Optional[str] = None,
    fetch: Optional[Callable[..., Awaitable[Optional[dict]]]] = None,
    threshold: float = 0.55,
    size: int = 600,
) -> Optional[str]:
    """回 iTunes 高清方形封面 URL；查不到/低信心/關閉 → 回 fallback。"""
    if not enabled() or not title:
        return fallback
    best = await _resolve_best(title, artist, fetch=fetch, threshold=threshold)
    if best is None:
        return fallback
    return _hi_res(_art(best), size)


async def resolve_metadata(
    title: str,
    artist: Optional[str] = None,
    *,
    fetch: Optional[Callable[..., Awaitable[Optional[dict]]]] = None,
    threshold: float = 0.55,
    size: int = 600,
) -> Optional[dict]:
    """單次 iTunes 查詢同時拿封面+演出者+專輯（跟 resolve_cover() 分開呼叫是兩次
    API request，這支給需要三者一起的呼叫端用，見 music_cog.py::_apply_itunes_cover）。
    回 {"cover":, "artist":, "album":}（沒配到欄位個別回 None）；整體查不到/低信心/
    關閉 → 回 None，呼叫端自行決定退回值。"""
    if not enabled() or not title:
        return None
    best = await _resolve_best(title, artist, fetch=fetch, threshold=threshold)
    if best is None:
        return None
    art = _art(best)
    return {
        "cover": _hi_res(art, size) if art else None,
        "artist": best.get("artistName") or None,
        "album": best.get("collectionName") or None,
    }
