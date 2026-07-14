"""
car_presence.py — 車載 presence 狀態機（ESP32 puck）。

純邏輯、無 I/O、無 Discord：注入 on_arrive / on_depart callback + 時鐘，好測。

契約（design doc + eng review）：
- present() 到達時觸發 on_arrive 一次；後續 present() 只續 heartbeat、不重觸發（debounce）。
- 熄火斷電＝puck 停送 heartbeat；check_ttl() 逾 TTL → 視為 absent、觸發 on_depart。
  （∴ present 不 sticky——puck 永遠不會主動送 absent，靠 TTL 收尾。）
- absent() 主動離開 → 觸發 on_depart（MVP：不寫記憶，由 on_depart 決定停播動作）。
"""
from __future__ import annotations

import time
from typing import Awaitable, Callable


class CarPresence:
    def __init__(
        self,
        *,
        on_arrive: Callable[[], Awaitable[None]],
        on_depart: Callable[[], Awaitable[None]],
        ttl_s: float = 90.0,
        time_fn: Callable[[], float] = time.monotonic,
    ):
        self._on_arrive = on_arrive
        self._on_depart = on_depart
        self._ttl_s = ttl_s
        self._time = time_fn
        self._present = False
        self._last_hb = 0.0

    @property
    def is_present(self) -> bool:
        return self._present

    async def present(self) -> None:
        """puck boot / heartbeat。到達觸發開場一次；heartbeat 只續期。"""
        self._last_hb = self._time()
        if not self._present:
            self._present = True
            await self._on_arrive()

    async def absent(self) -> None:
        """主動離開（若 puck 有機會送）。只在原本 present 時觸發停播。"""
        if self._present:
            self._present = False
            await self._on_depart()

    async def check_ttl(self) -> bool:
        """定期由背景驅動：heartbeat 逾 TTL（熄火斷電）→ 視為 absent、停播。

        回 True＝這次判定逾時並觸發了 on_depart。
        """
        if self._present and (self._time() - self._last_hb) > self._ttl_s:
            self._present = False
            await self._on_depart()
            return True
        return False
