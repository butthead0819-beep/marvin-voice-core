"""ESP32 車puck edge端混音的控制指令佇列（pull model）。

Pi mk2 走 push model：Mac 直接 POST 到 Pi 的 /puck/*（見 puck_mixer_client.py），因為 Pi
在 LAN 內、可被動接收連線。ESP32 car puck 只在熱點/Funnel 後面，永遠是它主動撥出連線
（car_puck.ino 的 audioNetworkTask/carHeartbeat 都是 client connect 出去，從沒 listen）
——Mac 沒辦法主動推指令進去，只能讓 ESP32 用既有心跳節奏順便輪詢。

跟 BrowserSpeakerOutput.latest_wav() 同一種 seq 模式：push() 累加 seq，since(seq) 回傳
seq 之後的所有指令（可能不只一筆——ESP32 輪詢間隔內 Mac 可能連續下了 queue_next +
crossfade 兩個指令，兩個都要送到，不能只回最新一筆）。
"""
from __future__ import annotations

import threading


class PuckCommandQueue:
    def __init__(self, *, max_history: int = 50):
        self._lock = threading.Lock()
        self._commands: list[dict] = []   # [{"seq":..., "cmd":..., ...}]
        self._seq = 0
        self._max_history = max_history

    def _push(self, cmd: dict) -> int:
        with self._lock:
            self._seq += 1
            entry = {"seq": self._seq, **cmd}
            self._commands.append(entry)
            if len(self._commands) > self._max_history:
                self._commands = self._commands[-self._max_history:]
            return self._seq

    def play(self, url: str) -> int:
        return self._push({"cmd": "play", "url": url})

    def queue_next(self, url: str) -> int:
        return self._push({"cmd": "queue_next", "url": url})

    def crossfade(self, duration_s: float = 4.0) -> int:
        return self._push({"cmd": "crossfade", "duration_s": duration_s})

    def stop(self) -> int:
        return self._push({"cmd": "stop"})

    def since(self, seq: int) -> tuple[int, list[dict]]:
        """回傳 (目前最新 seq, seq 之後的所有指令，舊到新排序)。

        seq 早於歷史保留範圍（超過 max_history 被砍掉）時，回傳目前存著的全部
        歷史——寧可讓 ESP32 收到「補播」幾個舊指令，也不要靜默漏掉。"""
        with self._lock:
            pending = [c for c in self._commands if c["seq"] > seq]
            return self._seq, pending


# 同進程內的 lazy singleton：main_satellite.py（HTTP handler，讀）跟 music_cog.py
# （_run_tail_dj，寫）各自 import 這個模組取同一份 queue，不用額外把物件穿過一堆
# 函式參數傳遞——跟 music_cog.py 既有的 `_puck_mixer_client` lazy singleton 同一種作法。
_default_queue: PuckCommandQueue | None = None


def get_default_queue() -> PuckCommandQueue:
    global _default_queue
    if _default_queue is None:
        _default_queue = PuckCommandQueue()
    return _default_queue


class PuckCommandQueueClient:
    """跟 puck_mixer_client.PuckMixerClient 同款 async 介面（play/queue_next/crossfade/
    stop 都回 bool），讓 music_cog.py::_fire_puck_crossfade 等既有呼叫端不用分辨背後是
    HTTP POST 到 Pi（push）還是寫進本地 PuckCommandQueue（ESP32 pull，見上）。"""

    def __init__(self, queue: PuckCommandQueue):
        self._queue = queue

    async def play(self, url: str) -> bool:
        self._queue.play(url)
        return True

    async def queue_next(self, url: str) -> bool:
        self._queue.queue_next(url)
        return True

    async def crossfade(self, duration_s: float = 4.0) -> bool:
        self._queue.crossfade(duration_s)
        return True

    async def stop(self) -> bool:
        self._queue.stop()
        return True
