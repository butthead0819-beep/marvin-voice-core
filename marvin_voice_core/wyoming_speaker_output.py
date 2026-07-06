"""衛星播放 adapter：泵執行緒 write(48k stereo s16 frame) → event loop 送 AudioChunk。

實體音箱 S4-4.2。LocalSpeakerDevice 的 output= 注入點吃任何有 write(frame)/close()
的物件；本 adapter 把 mixer 泵的音訊幀轉成 wyoming AudioChunk，透過已連上衛星的
WyomingSatelliteBridge._client 送回 Pi（衛星 snd-command: aplay -r 48000 -c 2）。

執行緒界線：write()/close() 由 LocalSpeakerDevice 泵執行緒（非 loop 執行緒）呼叫，故
一律 loop.call_soon_threadsafe 跨回 event loop；真正送事件在 loop 上的 _drain task。

已知取捨（先求通）：
  - 持續泵＝AudioStart 一次後持續送流（含靜音幀，~1.5Mbps）。2.4G WiFi 可承受；不穩
    再做 idle 停送優化。
  - _drain 啟動時快取 _client 一次：衛星斷線→重連（bridge 換新 _client）後，本 task
    仍寫舊 client。S4 live 若遇重連掉音，重建 speaker device 即可；idle 停送優化會一併
    解掉。此限制不擋端到端點火。
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class WyomingSpeakerOutput:
    def __init__(self, bridge, loop: asyncio.AbstractEventLoop):
        self._bridge = bridge
        self._loop = loop
        self._q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._task: asyncio.Task | None = None

    def _ensure_task(self) -> None:
        if self._task is None:
            self._task = self._loop.create_task(self._drain())

    async def _drain(self) -> None:
        from wyoming.audio import AudioChunk, AudioStart  # noqa: PLC0415

        c = self._bridge._client
        if c is None:
            return
        await c.write_event(AudioStart(rate=48000, width=2, channels=2).event())
        while True:
            frame = await self._q.get()
            if frame is None:   # close() 哨兵
                break
            await c.write_event(
                AudioChunk(rate=48000, width=2, channels=2, audio=frame).event())

    def write(self, frame: bytes) -> None:   # 泵執行緒呼叫
        self._loop.call_soon_threadsafe(self._ensure_task)
        try:
            self._loop.call_soon_threadsafe(self._q.put_nowait, frame)
        except Exception:   # noqa: BLE001
            pass            # 佇列滿＝網路塞，丟幀別堆積延遲

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._q.put_nowait, None)
