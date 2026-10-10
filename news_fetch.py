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


def news_url(keyword: str) -> str:
    """查該 keyword 的 Google News RSS 搜尋網址。"""
    return f"{RSS_BASE}/search?q={quote(keyword)}&{_LOCALE}"


async def _default_fetch(keyword: str, *, timeout_s: float = 6.0) -> Optional[str]:
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
    """抓最多 limit 則新聞標題，查 keyword。沒有 keyword 不抓（回 []）。
    剝完來源後綴、過濾不安全標題、同標題只留第一個，保留 RSS 順序。
    任何失敗回 []。"""
    if not enabled():
        return []
    if not keyword:
        return []
    xml_text = await (fetch or _default_fetch)(keyword)
    if not xml_text:
        return []
    return _parse_items(xml_text, limit)


def pick_news_interest(interests: Sequence[str], *, rng=random) -> Optional[tuple[str, str]]:
    """在場興趣平均抽一個，回 (興趣原句, 搜尋關鍵字)；興趣原句給 render 講「講給誰
    聽」。interests 元素格式是 "{成員名}喜歡{東西}"：含「喜歡」取其後的部分當關鍵字，
    不含則原樣使用。沒有候選回 None。"""
    candidates: list[tuple[str, str]] = []
    seen_keywords: set[str] = set()
    for item in interests or []:
        s = str(item).strip()
        if "喜歡" in s:
            keyword = s.split("喜歡", 1)[-1].strip()
        else:
            keyword = s
        if not keyword or keyword in seen_keywords:
            continue
        seen_keywords.add(keyword)
        candidates.append((s, keyword))
    if not candidates:
        return None
    return rng.choice(candidates)
