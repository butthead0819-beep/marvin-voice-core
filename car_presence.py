"""
car_presence.py — 車載 presence 狀態機（ESP32 puck）。

純邏輯、無 I/O、無 Discord：注入 on_arrive / on_depart callback + 時鐘，好測。

契約（多人同車）：
- 每個 speaker 各自帶心跳時間；名單依抵達順序（dict 插入順序）。
- 車上從「空」變「有人」才觸發 on_arrive(speaker)（第一個到的人＝開場選歌的依據）；
  已有人在場時，新到的人只加入名單、不重觸發開場。
- 名單從「有人」變「空」才觸發 on_depart()（最後一個走＝停播）。
- 熄火斷電＝puck 停送 heartbeat；check_ttl() 逾 TTL 的人各自移除，名單清空才停播。
  （∴ present 不 sticky——puck 永遠不會主動送 absent，靠 TTL 收尾。）
- absent(speaker) 主動離開：只移除這個人；名單清空才停播。
"""
from __future__ import annotations

import time
from typing import Awaitable, Callable


class CarPresence:
    def __init__(
        self,
        *,
        on_arrive: Callable[[str], Awaitable[None]],
        on_depart: Callable[[], Awaitable[None]],
        ttl_s: float = 90.0,
        time_fn: Callable[[], float] = time.monotonic,
    ):
        self._on_arrive = on_arrive
        self._on_depart = on_depart
        self._ttl_s = ttl_s
        self._time = time_fn
        # speaker → 最後一次心跳時間；dict 插入順序＝抵達順序
        self._occupants: dict[str, float] = {}

    @property
    def is_present(self) -> bool:
        return bool(self._occupants)

    @property
    def occupants(self) -> list[str]:
        """在場者，依抵達順序。"""
        return list(self._occupants)

    async def present(self, speaker: str = "") -> None:
        """puck boot / heartbeat。車上從空變有人才觸發開場一次；heartbeat 只續期。"""
        was_empty = not self._occupants
        self._occupants[speaker] = self._time()
        if was_empty:
            await self._on_arrive(speaker)

    async def absent(self, speaker: str = "") -> None:
        """主動離開（若 puck 有機會送）。只移除這個人；名單清空才停播。"""
        if speaker not in self._occupants:
            return
        del self._occupants[speaker]
        if not self._occupants:
            await self._on_depart()

    async def check_ttl(self) -> bool:
        """定期由背景驅動：各人 heartbeat 逾 TTL（熄火斷電）→ 移除；名單因此清空→停播。

        回 True＝這次判定造成名單從有人變空，並觸發了 on_depart。
        """
        now = self._time()
        expired = [s for s, last in self._occupants.items() if (now - last) > self._ttl_s]
        if not expired:
            return False
        for s in expired:
            del self._occupants[s]
        if not self._occupants:
            await self._on_depart()
            return True
        return False
