"""Google News RSS 新聞抓取：免費、不需 API key，給 DJ 口白 news mode 當素材用。

fetch 可注入（同 itunes_cover.py pattern），方便測試不真連網。任何失敗（逾時/非
200/XML 壞掉/沒 aiohttp）一律 fail-open 回 []，不擋主流程。
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import xml.etree.ElementTree as ET
from typing import Awaitable, Callable, Optional, Sequence
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


def _parse_items(xml_text: str, limit: int) -> list[str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items = root.findall("./channel/item")
    if not items:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        clean_title = _strip_source_suffix(title)
        if not clean_title or not is_safe_news_title(clean_title):
            continue
        if clean_title in seen:
            continue
        seen.add(clean_title)
        out.append(clean_title)
        if len(out) >= limit:
            break
    return out


async def fetch_news_headlines(
    keyword: Optional[str] = None,
    *,
    fetch: Optional[Callable[..., Awaitable[Optional[str]]]] = None,
    limit: int = 5,
) -> list[str]:
    """抓最多 limit 則新聞標題。keyword 給時查該關鍵字，否則從天氣/科技/科學隨機挑來源
    （見 news_url）。剝完來源後綴、過濾不安全標題、同標題只留第一個，保留 RSS 順序。
    任何失敗回 []。"""
    if not enabled():
        return []
    xml_text = await (fetch or _default_fetch)(keyword)
    if not xml_text:
        return []
    return _parse_items(xml_text, limit)


def pick_news_keyword(interests: Sequence[str], *, rng=random) -> Optional[str]:
    """在場興趣關鍵字與三個預設來源平均抽一個。interests 元素格式是
    "{成員名}喜歡{東西}"：含「喜歡」取其後的部分，不含則原樣使用。回傳 None 代表
    交給 news_url 從 DEFAULT_SOURCES 隨機挑。"""
    keywords: list[str] = []
    seen: set[str] = set()
    for item in interests or []:
        s = str(item).strip()
        if "喜歡" in s:
            s = s.split("喜歡", 1)[-1].strip()
        if s and s not in seen:
            seen.add(s)
            keywords.append(s)
    if not keywords:
        return None
    candidates: list[Optional[str]] = keywords + [None] * len(DEFAULT_SOURCES)
    return rng.choice(candidates)
