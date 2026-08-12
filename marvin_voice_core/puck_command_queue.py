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
import time


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

    def speak(self, clip_id: str) -> int:
        """[PuckMixer Phase3] DJ 口白：ESP32 端收到會 duck 音樂音量再疊播（見
        car_puck.ino dispatchNewCommands 的 speak 分支 + mixOutputTask 的
        VOICE_DUCK_GAIN）。"""
        return self._push({"cmd": "speak", "clip_id": clip_id})

    def sfx(self, clip_id: str) -> int:
        """[PuckMixer Phase3] 轉場音效：不 duck，直接疊播（比照 STEP10 deck B
        scratch 疊加的感受，音樂音量不變）。"""
        return self._push({"cmd": "sfx", "clip_id": clip_id})

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


# ── Voice clip 登記表：DJ 口白/SFX 是本機檔案（TTS 暫存/gen_dj_sfx.py 產物），跟
# /puck_deck 的 YouTube URL 不同不能直接讓 ESP32 打 URL 過去解析——這裡登記成短效
# clip_id，main_satellite.py::handle_puck_voice 收到 clip_id 才轉譯回真實路徑轉碼給
# ESP32。不直接把檔案路徑暴露在 /car_commands 回應裡（那條走 Funnel 公開）。
_voice_clips: dict[str, tuple[str, float]] = {}
_voice_clip_seq = 0
_VOICE_CLIP_TTL_S = 300.0   # 5 分鐘沒被 ESP32 抓走就過期，避免字典無限長大


def register_voice_clip(path: str) -> str:
    """登記本機檔案路徑，回傳短效 clip_id。順便清掉已過期的舊 entry。"""
    global _voice_clip_seq
    now = time.time()
    for k in [k for k, (_, exp) in _voice_clips.items() if exp < now]:
        _voice_clips.pop(k, None)
    _voice_clip_seq += 1
    clip_id = str(_voice_clip_seq)
    _voice_clips[clip_id] = (path, now + _VOICE_CLIP_TTL_S)
    return clip_id


def resolve_voice_clip(clip_id: str) -> str | None:
    """clip_id → 檔案路徑；找不到/過期回 None。"""
    entry = _voice_clips.get(clip_id)
    if entry is None:
        return None
    path, exp = entry
    if exp < time.time():
        _voice_clips.pop(clip_id, None)
        return None
    return path


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

    async def speak(self, path: str) -> bool:
        """path＝本機 DJ 口白音檔（Discord/pi_bt 路徑既有的 audio_path）；這裡負責
        登記成 clip_id 再送 speak 指令，呼叫端不用知道 ESP32 這邊的傳輸細節。"""
        self._queue.speak(register_voice_clip(path))
        return True

    async def sfx(self, path: str) -> bool:
        self._queue.sfx(register_voice_clip(path))
        return True
