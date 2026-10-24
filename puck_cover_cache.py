"""車機封面預算快取。

info['thumbnail'] 是 iTunes 方圖（_apply_itunes_cover 換成功）或 YT 縮圖都做；
YT 縮圖的黑邊/裁正方形由 puck_cover 處理。
單一 URL（含尺寸）單飛：並發只抓一次；LRU 上限；失敗記住不重抓。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict

logger = logging.getLogger(__name__)


def cover_url_for(info) -> str | None:
    """info 為 dict 且有非空 thumbnail → 回 thumbnail；否則 None。"""
    if not isinstance(info, dict):
        return None
    url = info.get("thumbnail")
    return url or None


class PuckCoverCache:
    def __init__(self, *, fetch=None, render=None, max_items: int = 4, max_failed: int = 50):
        self._fetch = fetch or self._default_fetch
        self._render = render or (lambda data, size: self._default_render(data, size))
        self._max_items = max_items
        self._max_failed = max_failed
        self._items: OrderedDict = OrderedDict()
        self._failed: set = set()
        self._inflight: dict = {}

    @staticmethod
    def _default_render(data: bytes, size: int) -> bytes:
        import puck_cover
        return puck_cover.render_puck_cover(data, size=size)

    async def get(self, info, size: int = 466) -> bytes | None:
        url = cover_url_for(info)
        if url is None:
            return None
        key = (url, size)

        if key in self._items:
            self._items.move_to_end(key)
            return self._items[key]

        if key in self._failed:
            return None

        task = self._inflight.get(key)
        if task is not None:
            return await asyncio.shield(task)

        title = info.get("title", "")
        task = asyncio.ensure_future(self._build(key, url, size, title))
        self._inflight[key] = task
        try:
            return await asyncio.shield(task)
        finally:
            self._inflight.pop(key, None)

    async def _build(self, key, url: str, size: int, title: str) -> bytes | None:
        try:
            t0 = time.monotonic()
            data = await self._fetch(url)
            t1 = time.monotonic()
            jpg = await asyncio.to_thread(self._render, data, size)
            t2 = time.monotonic()
        except Exception as e:  # noqa: BLE001
            if len(self._failed) > self._max_failed:
                self._failed.clear()
            self._failed.add(key)
            logger.warning(f"⚠️ [PuckCover] 失敗 {title[:30]}: {e!r}")
            return None

        self._items[key] = jpg
        if len(self._items) > self._max_items:
            self._items.popitem(last=False)
        logger.info(
            f"🔘 [PuckCover] 生成 {title[:30]} {size}px {len(jpg) // 1024}KB "
            f"抓{int((t1 - t0) * 1000)}ms 算{int((t2 - t1) * 1000)}ms")
        return jpg

    @staticmethod
    async def _default_fetch(url: str) -> bytes:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                return await resp.read()
