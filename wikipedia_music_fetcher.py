"""維基百科免費開放 API 音樂背景擷取模組。

100% 免費、免 API Key、非同步短超時（1.5s）+ fail-open：
1. 查詢歌曲背景、創作靈感與時代歷史
2. 清理 Wiki 標記與外語註解，提煉精簡摘要
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

_WIKI_API_URL = "https://zh.wikipedia.org/w/api.php"
_TIMEOUT_SECONDS = 1.5


def parse_wikipedia_summary(extract: str) -> str | None:
    """清理維基百科 API 回傳之 extract 文本。"""
    if not extract:
        return None

    text = extract.strip()
    # 清理開頭常見的語言註解，例如（英語：Sunny Day）、（日語：...）
    text = re.sub(r"[（\(](?:英語|日語|韓語|法語|德語|義大利語|粵語)[：:][^）\)]*[）\)]", "", text)
    # 清理多餘空格與換行
    text = re.sub(r"\s+", " ", text).strip()
    
    # 若為消歧義頁面或太短則無效
    if "可以指：" in text or "消歧義" in text or len(text) < 15:
        return None

    # 擷取核心第一至二句（限 120 字內）
    return text[:120].strip()


async def _query_wikipedia_api(title_query: str) -> dict[str, Any] | None:
    """發送 HTTP GET 請求至中文維基百科 API。"""
    params = {
        "action": "query",
        "format": "json",
        "prop": "extracts",
        "exintro": "1",
        "explaintext": "1",
        "redirects": "1",
        "titles": title_query,
    }
    headers = {
        "User-Agent": "MarvinDiscordBot/2.0 (music_dj; open-source)",
    }
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(_WIKI_API_URL, params=params, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.json()
    except Exception as e:
        logger.debug(f"[WikiFetcher] 查詢 {title_query} 失敗或超時: {e}")
    return None


async def fetch_wikipedia_music_summary(title: str, artist: str = "") -> str | None:
    """非同步查詢維基百科中該歌曲的背景摘要（短超時 fail-open）。"""
    t = (title or "").strip()
    a = (artist or "").strip()
    if not t:
        return None

    # 構造候選標題（優先查詢歌曲專屬頁面）
    candidates = []
    if a:
        candidates.append(f"{t} ({a}歌曲)")
    candidates.append(f"{t} (歌曲)")
    candidates.append(t)

    for query in candidates:
        data = await _query_wikipedia_api(query)
        if not data:
            continue
        pages = data.get("query", {}).get("pages", {})
        for page_id, page_data in pages.items():
            if page_id != "-1":
                extract = page_data.get("extract", "")
                parsed = parse_wikipedia_summary(extract)
                if parsed:
                    return parsed

    return None
