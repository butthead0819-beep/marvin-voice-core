"""車載 puck streaming 播放 adapter：mixer 泵 write(48k stereo s16 frame) → 即時廣播。

實體音箱 mk2（ESP32 car puck）。LocalSpeakerDevice 的 output= 注入點吃任何有
write(frame)/close() 的物件；本 adapter 跟 BrowserSpeakerOutput 不同——不做靜音切段
緩衝整段 wav 再回，而是逐 frame 即時 fan-out 給已連線的 /audio_stream client（chunked
HTTP），讓下游（ESP32 PCM5102）能像收音機一樣連續播放整份歌單，不受單段緩衝上限限制。

執行緒界線：write()/close() 由 LocalSpeakerDevice 泵執行緒（非 loop 執行緒）呼叫，故
一律 loop.call_soon_threadsafe 跨回 event loop 廣播給訂閱者佇列；訂閱/退訂/消化佇列
都在 event loop 上（aiohttp handler）執行。

多訂閱者 fan-out：同時多個 client 連 /audio_stream 都會收到同一份 mixer 輸出。單一慢
client 佇列滿了就丟該 client 的幀（不回壓 mixer 泵執行緒、不拖累其他訂閱者）。
"""
from __future__ import annotations

import asyncio

import numpy as np


class StreamSpeakerOutput:
    def __init__(self, loop: asyncio.AbstractEventLoop, *,
                 rate: int = 48000, channels: int = 2, bits: int = 16,
                 mono_downmix: bool = False):
        """mono_downmix=True：write() 收到的 stereo s16 frame 先平均左右聲道降成 mono
        再廣播（見 music_request_latency_anatomy 系列 debug——車 puck 只有一顆喇叭，
        傳 stereo 純粹浪費頻寬；離家用 4G 走 Tailscale Funnel 時 CPU 軟體 TLS 解密量
        減半，直接换 throughput 餘裕）。channels 屬性同步報 1，給 /audio_stream
        的 X-Audio-Channels 標頭對齊。"""
        self._loop = loop
        self.rate = rate
        self.channels = 1 if mono_downmix else channels
        self.bits = bits
        self._mono_downmix = mono_downmix
        self._subscribers: set[asyncio.Queue] = set()

    def write(self, frame: bytes) -> None:   # 泵執行緒呼叫
        if not frame:
            return
        if self._mono_downmix:
            stereo = np.frombuffer(frame, dtype="<i2").reshape(-1, 2).astype(np.int32)
            frame = ((stereo[:, 0] + stereo[:, 1]) // 2).astype("<i2").tobytes()
        self._loop.call_soon_threadsafe(self._broadcast, frame)

    def close(self) -> None:   # 泵執行緒呼叫
        self._loop.call_soon_threadsafe(self._broadcast, None)

    def _broadcast(self, frame) -> None:   # loop 執行緒
        for q in list(self._subscribers):
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                pass   # 慢訂閱者丟這幀，不堆延遲、不回壓、不影響其他訂閱者

    def subscribe(self) -> asyncio.Queue:
        """event loop 執行緒呼叫（aiohttp handler）。回傳專屬佇列，讀到 None＝上游關閉。"""
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)
