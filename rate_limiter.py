"""
rate_limiter.py — 固定視窗 per-key 限速器（/audio 公開後防付費灌爆）。

純邏輯、無 I/O：注入時鐘，好測。key＝token（或 IP fallback），
每個 key 各自一個固定視窗計數，超過 max_per_window → allow() 回 False。
"""
from __future__ import annotations

import time
from typing import Callable


class RateLimiter:
    def __init__(
        self,
        *,
        max_per_window: int,
        window_s: float,
        time_fn: Callable[[], float] = time.monotonic,
    ):
        self._max = max_per_window
        self._window_s = window_s
        self._time = time_fn
        self._hits: dict[str, tuple[float, int]] = {}   # key → (window_start, count)

    def allow(self, key: str) -> bool:
        now = self._time()
        start, count = self._hits.get(key, (now, 0))
        if now - start >= self._window_s:     # 視窗過期 → 重開
            start, count = now, 0
        count += 1
        self._hits[key] = (start, count)
        return count <= self._max
