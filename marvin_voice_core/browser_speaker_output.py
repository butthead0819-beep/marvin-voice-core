"""純軟體 satellite 播放 adapter：mixer 泵 write(48k stereo s16 frame) → 靜音切段 → /reply。

與 wyoming_speaker_output.WyomingSpeakerOutput 對稱（同吃 LocalSpeakerDevice 的 output=
注入點：write(frame)/close()），但不送網路。改用靜音偵測把 mixer 連續泵的音訊幀切成
「一段回覆」快取起來，由 main_satellite 的 GET /reply 編成 WAV 給瀏覽器 WebAudio 播。

執行緒界線：write()/close() 由 LocalSpeakerDevice 泵執行緒呼叫；latest_wav() 由 event
loop 上的 aiohttp handler 呼叫。以 threading.Lock 保護跨執行緒讀取的 _seq/_latest。

切段（不需時鐘，用幀數＝穩定可測）：
  - 幀振幅 ≥ _silence_threshold → 語音，累積。
  - 已累積語音後連續 _hangover_frames 幀靜音 → 定案（seq+1、清空、等下一段）。
  - close() 對未定案的尾巴強制 flush，不遺失最後一句。
"""
from __future__ import annotations

import struct
import threading


class BrowserSpeakerOutput:
    def __init__(self, *, silence_threshold: int = 30, hangover_frames: int = 15,
                 rate: int = 48000, channels: int = 2):
        self._silence_threshold = silence_threshold
        self._hangover_frames = hangover_frames
        self._rate = rate
        self._channels = channels
        self._current = bytearray()
        self._has_audio = False
        self._silence_run = 0
        self._latest = b""
        self._seq = 0
        self._lock = threading.Lock()

    @staticmethod
    def _max_amp(frame: bytes) -> int:
        n = len(frame) // 2
        if n == 0:
            return 0
        peak = 0
        for s in struct.unpack("<%dh" % n, frame[: n * 2]):
            a = -s if s < 0 else s
            if a > peak:
                peak = a
        return peak

    def write(self, frame: bytes) -> None:   # 泵執行緒呼叫
        if not frame:
            return
        if self._max_amp(frame) >= self._silence_threshold:
            self._current.extend(frame)
            self._has_audio = True
            self._silence_run = 0
        elif self._has_audio:
            self._silence_run += 1
            if self._silence_run >= self._hangover_frames:
                self._finalize()

    def _finalize(self) -> None:
        if not self._current:
            self._has_audio = False
            self._silence_run = 0
            return
        with self._lock:
            self._latest = bytes(self._current)
            self._seq += 1
        self._current = bytearray()
        self._has_audio = False
        self._silence_run = 0

    def close(self) -> None:
        self._finalize()   # flush 未定案尾巴

    def latest_wav(self) -> tuple[int, bytes]:
        """回 (seq, wav_bytes)。seq=0＝尚無回覆；seq 每定案一段 +1。"""
        with self._lock:
            seq, pcm = self._seq, self._latest
        if seq == 0:
            return 0, b""
        return seq, self._wrap_wav(pcm)

    def _wrap_wav(self, pcm: bytes) -> bytes:
        rate, ch, bits = self._rate, self._channels, 16
        byte_rate = rate * ch * bits // 8
        block_align = ch * bits // 8
        return b"".join([
            b"RIFF", struct.pack("<I", 36 + len(pcm)), b"WAVE",
            b"fmt ", struct.pack("<IHHIIHH", 16, 1, ch, rate, byte_rate, block_align, bits),
            b"data", struct.pack("<I", len(pcm)), pcm,
        ])
