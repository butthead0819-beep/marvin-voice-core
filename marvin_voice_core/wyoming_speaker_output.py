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
import os

import numpy as np

logger = logging.getLogger(__name__)

# s16 振幅低於此＝視為靜音（含 mixer TPDF dither），不送網路。idle 泵持續送靜音會塞爆
# 佇列→真語音幀被丟→aplay 把剩餘樣本接連播、時間軸壓縮＝聽起來「超級快」。只在有實際
# 聲音時才送＝idle 停送，讓語音幀走乾淨的空佇列、不被丟。
_SILENCE_MAX = 30

# 網路送出格式。預設 **48k stereo（1.9Mbps，高音質）**＝給有線輸出（3.5mm 耳機孔 / DigiAMP+
# I2S）：本地即時消化、無 TCP 背壓、Pi WiFi 不被 BT 搶。
# `MARVIN_SATELLITE_LOWBW=1` → **16k mono（256kbps）**＝BT 喇叭專用 workaround（Pi3B WiFi↔BT
# 共天線，1.9Mbps 連續串流會被 BT 卡頓→掉幀/背壓凍死）；語音幾乎無損、音樂有損，Pi 端要配
# up.py 升回 48k stereo。連續音樂走 BT 會背壓凍死（下游太慢）——BT 只適合短語音。
_LOWBW = os.getenv("MARVIN_SATELLITE_LOWBW", "").strip().lower() in ("1", "true", "yes", "on")
_SEND_RATE = 16000 if _LOWBW else 48000
_SEND_CH = 1 if _LOWBW else 2

# 輸出補償增益。音量正解＝控制喇叭端（BT/AVRCP 音量），軟體 gain 會 clip 失真，故 1.0＝
# 不動訊號。留旋鈕在此備用（device-only，Discord 不受影響）。
_TTS_BOOST = 1.0


def _to_16k_mono(frame: bytes) -> bytes:
    """48k stereo s16 → 16k mono s16（downmix + box-filter 抗鋸齒降取樣 /3 + 補償增益）。"""
    a = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
    a = a[: a.size - (a.size % 2)]          # 對齊 stereo
    if a.size == 0:
        return b""
    mono48 = a.reshape(-1, 2).mean(axis=1)  # L+R 平均＝downmix
    n = (mono48.size // 3) * 3
    if n == 0:
        return b""
    mono16 = mono48[:n].reshape(-1, 3).mean(axis=1) * _TTS_BOOST  # 降 48k→16k + 補償小聲
    return np.clip(mono16, -32768, 32767).astype(np.int16).tobytes()


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

        # ⚠️ 每圈重讀 self._bridge._client（不快取）：衛星斷線→重連換新 client 後，若寫舊
        # 的死 client，write_event 會卡住/失敗→_drain 停消化→佇列爆(QueueFull)→Pi 餓死。
        # 換 client 時重送 AudioStart（新串流）；送失敗就重置、下圈重來。
        started = None
        while True:
            frame = await self._q.get()
            if frame is None:   # close() 哨兵
                break
            c = self._bridge._client
            if c is None:
                started = None
                continue
            try:
                if started is not c:
                    await c.write_event(AudioStart(rate=_SEND_RATE, width=2, channels=_SEND_CH).event())
                    started = c
                await c.write_event(
                    AudioChunk(rate=_SEND_RATE, width=2, channels=_SEND_CH, audio=frame).event())
            except Exception:   # noqa: BLE001
                started = None   # 寫失敗（client 死了）→下圈重讀新 client + 重送 AudioStart

    def write(self, frame: bytes) -> None:   # 泵執行緒呼叫
        if not frame:
            return
        # idle 停送：靜音幀不送網路（見 _SILENCE_MAX），避免 flood 塞爆佇列丟掉真語音幀。
        try:
            if int(np.abs(np.frombuffer(frame, dtype=np.int16)).max()) < _SILENCE_MAX:
                return
        except Exception:   # noqa: BLE001
            pass            # 判斷失敗就照送，不擋音
        out = _to_16k_mono(frame) if _LOWBW else frame   # 有線＝原生 48k stereo；BT＝降 16k mono
        if not out:
            return
        self._loop.call_soon_threadsafe(self._ensure_task)
        self._loop.call_soon_threadsafe(self._enqueue, out)

    def _enqueue(self, frame: bytes) -> None:   # loop 執行緒；優雅丟幀（put_nowait 在此才真跑）
        try:
            self._q.put_nowait(frame)
        except asyncio.QueueFull:
            pass            # 網路塞→丟這幀，不堆延遲、不噴 traceback

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._q.put_nowait, None)
