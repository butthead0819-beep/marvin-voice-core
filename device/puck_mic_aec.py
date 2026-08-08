"""
puck_mic_aec.py — INMP441 麥克風擷取迴圈 + 即時 AEC（車 puck，PoC）。

跟 puck_mixer.PuckMixer 同一個 process 內執行（見 volume_server.py 的
module-level _puck_mixer 單例），直接呼叫 mixer.recent_reference() 拿
reference，不用額外 IPC/socket。

喚醒詞引擎車 puck 這邊還沒定案，這裡刻意用 on_clean_chunk callback 把
「AEC 前端」跟「喚醒詞後端」切開——預設實作寫進一個 named pipe，讓後續
不管接 openWakeWord 還是別的引擎，都只要 `cat` 這個 pipe 或直接開檔讀。

⚠️ 未在真實 INMP441 硬體上驗證，硬體到位前先確保 process_chunk() 這段
核心邏輯（抓 reference＋跑 AEC）本身正確，ALSA 擷取迴圈本身無法在沒有
硬體的機器上測試。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

import numpy as np

from device.puck_aec import MIC_RATE, PuckAecProcessor

try:
    import alsaaudio
except ImportError:  # 開發機沒有 pyalsaaudio，允許 import 供單元測試 process_chunk()
    alsaaudio = None

logger = logging.getLogger(__name__)

CHUNK_MS = 100.0
CHUNK_FRAMES = int(MIC_RATE * CHUNK_MS / 1000.0)  # 1600 frames @16kHz = 100ms
REF_MARGIN_S = 0.25  # reference 要比 mic chunk 多抓的餘裕，蓋過 AEC 內部延遲搜尋範圍


def make_fifo_writer(fifo_path: str) -> Callable[[bytes], None]:
    """回傳一個 callback：把清乾淨的 PCM bytes 寫進 named pipe。
    沒有讀取端在等（BrokenPipe/找不到 reader）時吞掉錯誤，不讓擷取迴圈掛掉。"""
    if not os.path.exists(fifo_path):
        os.mkfifo(fifo_path)
    state = {"fh": None}

    def _write(data: bytes):
        try:
            if state["fh"] is None:
                # O_NONBLOCK：沒有 reader 開著就別卡住整個擷取迴圈
                fd = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
                state["fh"] = os.fdopen(fd, "wb")
            state["fh"].write(data)
            state["fh"].flush()
        except (BrokenPipeError, OSError) as e:
            logger.debug(f"[PuckMicAec] fifo 寫入略過（可能沒有 reader）: {e}")
            state["fh"] = None

    return _write


class PuckMicAecLoop(threading.Thread):
    """背景執行緒：INMP441 擷取 → AEC → on_clean_chunk callback。呼叫 stop() 結束。"""

    def __init__(
        self,
        mixer,
        mic_device: str,
        on_clean_chunk: Callable[[bytes], None],
        aec: PuckAecProcessor | None = None,
    ):
        super().__init__(daemon=True, name="PuckMicAecLoop")
        self._mixer = mixer
        self._mic_device = mic_device
        self._on_clean_chunk = on_clean_chunk
        self._aec = aec or PuckAecProcessor()
        self._stop_flag = threading.Event()

    def stop(self):
        self._stop_flag.set()

    def process_chunk(self, mic_chunk: np.ndarray) -> np.ndarray:
        """擷取迴圈跟測試共用的核心邏輯：抓對應時間窗的 reference、跑 AEC。"""
        ref_seconds = len(mic_chunk) / MIC_RATE + REF_MARGIN_S
        ref = self._mixer.recent_reference(ref_seconds)
        return self._aec.process(mic_chunk, ref)

    def run(self):
        if alsaaudio is None:
            raise RuntimeError("alsaaudio 未安裝——這支迴圈只能在裝了 pyalsaaudio 的 Pi 上跑")
        pcm = alsaaudio.PCM(
            alsaaudio.PCM_CAPTURE, alsaaudio.PCM_NORMAL, device=self._mic_device,
            channels=1, rate=MIC_RATE, format=alsaaudio.PCM_FORMAT_S16_LE,
            periodsize=CHUNK_FRAMES,
        )
        try:
            while not self._stop_flag.is_set():
                length, data = pcm.read()
                if length <= 0:
                    time.sleep(0.01)
                    continue
                mic_chunk = np.frombuffer(data, dtype=np.int16).astype(np.float64)
                cleaned = self.process_chunk(mic_chunk)
                clipped = np.clip(cleaned, -32768, 32767).astype(np.int16)
                self._on_clean_chunk(clipped.tobytes())
        finally:
            pcm.close()
