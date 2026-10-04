"""Google News RSS 新聞抓取：免費、不需 API key，給長時間靜默時的新聞播報素材用。

fetch 可注入（同 itunes_cover.py pattern），方便測試不真連網。任何失敗（逾時/非
200/XML 壞掉/沒 aiohttp）一律 fail-open 回 None，不擋主流程。
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import xml.etree.ElementTree as ET
from typing import Awaitable, Callable, Collection, Optional
from urllib.parse import quote

try:
    import aiohttp
except ImportError:
    aiohttp = None

logger = logging.getLogger(__name__)

RSS_BASE = "https://news.google.com/rss"


def enabled() -> bool:
    return os.getenv("MARVIN_NEWS_BROADCAST", "1") == "1"


_LOCALE = "hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
# 10/4：沒興趣關鍵字時不抓熱門頭條（實測最近 25 則約一半是選舉政治/戰爭/社會案件，
# 關鍵字黑名單擋不完），改從天氣／科技／科學三個來源隨機挑；黑名單仍當第二層。
DEFAULT_SOURCES = (
    f"{RSS_BASE}/search?q={quote('天氣')}&{_LOCALE}",
    f"{RSS_BASE}/headlines/section/topic/TECHNOLOGY?{_LOCALE}",
    f"{RSS_BASE}/headlines/section/topic/SCIENCE?{_LOCALE}",
)


def news_url(keyword: Optional[str], *, rng=random) -> str:
    """keyword 給時查該關鍵字，否則從 DEFAULT_SOURCES 隨機挑一個。"""
    if keyword:
        return f"{RSS_BASE}/search?q={quote(keyword)}&{_LOCALE}"
    return rng.choice(DEFAULT_SOURCES)


async def _default_fetch(keyword: Optional[str], *, timeout_s: float = 6.0) -> Optional[str]:
    if aiohttp is None:
        return None
    url = news_url(keyword)
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


_UNSAFE_NEWS_KEYWORDS = (
    "死", "傷", "亡", "殺", "撞", "車禍", "命危", "搶救", "墜樓", "自焚", "輕生", "溺水", "失蹤",
    "性侵", "偷拍", "猥褻", "家暴", "通緝", "詐騙", "吸毒", "毒品", "開槍", "槍擊", "砍人",
    "政治", "選舉", "選戰", "立委", "民進黨", "國民黨", "民眾黨", "政黨", "立院", "立法院",
    "柯文哲", "賴清德", "藍綠", "罷免", "貪污", "表決", "抗議", "衝突",
    "外遇", "偷吃", "劈腿", "婚變", "八卦", "醜聞", "性騷"
)


def is_safe_news_title(title: str) -> bool:
    """檢查新聞標題是否適合電台播報（過濾社會悲劇、傷亡、政治及八卦）。"""
    t = (title or "").strip()
    if not t:
        return False
    return not any(kw in t for kw in _UNSAFE_NEWS_KEYWORDS)


def _parse_first_item(xml_text: str, exclude: Collection[str] = ()) -> Optional[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    items = root.findall("./channel/item")
    if not items:
        return None
    for item in items:
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        clean_title = _strip_source_suffix(title)
        if not clean_title or not is_safe_news_title(clean_title):
            continue
        if clean_title in exclude:
            continue
        return {"title": clean_title}
    return None


async def fetch_news_headline(
    keyword: Optional[str] = None,
    *,
    fetch: Optional[Callable[..., Awaitable[Optional[str]]]] = None,
    exclude: Collection[str] = (),
) -> Optional[dict]:
    """抓一則新聞標題。keyword 給時查該關鍵字，否則從天氣/科技/科學隨機挑來源（見 news_url）。失敗回 None。"""
    if not enabled():
        return None
    xml_text = await (fetch or _default_fetch)(keyword)
    if not xml_text:
        return None
    return _parse_first_item(xml_text, exclude=exclude)
