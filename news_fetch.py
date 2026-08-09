"""Google News RSS 新聞抓取：免費、不需 API key，給長時間靜默時的新聞播報素材用。

fetch 可注入（同 itunes_cover.py pattern），方便測試不真連網。任何失敗（逾時/非
200/XML 壞掉/沒 aiohttp）一律 fail-open 回 None，不擋主流程。
"""
from __future__ import annotations

import asyncio
import logging
import os
import xml.etree.ElementTree as ET
from typing import Awaitable, Callable, Optional
from urllib.parse import quote

try:
    import aiohttp
except ImportError:
    aiohttp = None

logger = logging.getLogger(__name__)

RSS_BASE = "https://news.google.com/rss"


def enabled() -> bool:
    return os.getenv("MARVIN_NEWS_BROADCAST", "1") == "1"


async def _default_fetch(keyword: Optional[str], *, timeout_s: float = 6.0) -> Optional[str]:
    if aiohttp is None:
        return None
    if keyword:
        url = f"{RSS_BASE}/search?q={quote(keyword)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    else:
        url = f"{RSS_BASE}?hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=timeout_s)) as resp:
                if resp.status != 200:
                    return None
                return await resp.text()
    except (aiohttp.ClientError, asyncio.TimeoutError, Exception):
        return None


def _strip_source_suffix(title: str) -> str:
    """Google News 標題常帶` - 來源名`後綴，念出來很怪，剝掉最後一段。"""
    if " - " in title:
        head, _, tail = title.rpartition(" - ")
        if head and 0 < len(tail) <= 20:
            return head.strip()
    return title.strip()


def _parse_first_item(xml_text: str) -> Optional[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    item = root.find("./channel/item")
    if item is None:
        return None
    title = (item.findtext("title") or "").strip()
    if not title:
        return None
    return {"title": _strip_source_suffix(title)}


async def fetch_news_headline(
    keyword: Optional[str] = None,
    *,
    fetch: Optional[Callable[..., Awaitable[Optional[str]]]] = None,
) -> Optional[dict]:
    """抓一則新聞標題。keyword 給時查該關鍵字，否則查台灣熱門頭條。失敗回 None。"""
    if not enabled():
        return None
    xml_text = await (fetch or _default_fetch)(keyword)
    if not xml_text:
        return None
    return _parse_first_item(xml_text)
