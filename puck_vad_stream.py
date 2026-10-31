"""連續音訊切句器 — 把 puck 全時收音的 VAD 機率序列切成語音段事件。

純邏輯，不做 I/O、不碰 asyncio。狀態機：

IDLE：
  window prob >= start_prob → 候選計數 +1（候選 window 的 PCM 暫存），
  否則候選計數歸零（暫存的候選 PCM 移入 pre-roll ring）。
  候選計數達 min_speech_windows → 轉 SPEAKING，呼叫
  on_speech_start(pre_roll ring 內容 + 候選 PCM)。

SPEAKING：
  每個 window 呼叫 on_pcm 並 append 到 segment。
  prob < end_prob → 靜音計數 +1，否則歸零。
  靜音計數達 end_silence_windows → on_speech_end(segment, "silence")，回 IDLE。
  segment window 數達 max_speech_windows → on_speech_end(segment, "max_len")，回 IDLE。

回 IDLE 時清空 pre-roll ring 與候選。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

WINDOW_SAMPLES = 512


@dataclass
class PuckVADConfig:
    start_prob: float = 0.5
    end_prob: float = 0.35
    min_speech_windows: int = 8
    end_silence_windows: int = 22
    pre_roll_windows: int = 13
    max_speech_windows: int = 469


class PuckVADStream:
    def __init__(
        self,
        vad,
        config: Optional[PuckVADConfig] = None,
        on_speech_start: Optional[Callable[[bytes], None]] = None,
        on_pcm: Optional[Callable[[bytes], None]] = None,
        on_speech_end: Optional[Callable[[bytes, str], None]] = None,
    ) -> None:
        self.vad = vad
        self.config = config or PuckVADConfig()
        self.on_speech_start = on_speech_start
        self.on_pcm = on_pcm
        self.on_speech_end = on_speech_end

        self._residual = np.empty(0, dtype=np.int16)

        self._speaking = False
        self._pre_roll: deque[bytes] = deque(maxlen=self.config.pre_roll_windows)
        self._candidate_count = 0
        self._candidate_pcm: list[bytes] = []
        self._silence_count = 0
        self._segment: list[bytes] = []

        self._windows = 0
        self._speech_windows = 0
        self._segments = 0
        self._suppressed = False

    @property
    def stats(self) -> dict:
        return {
            "windows": self._windows,
            "speech_windows": self._speech_windows,
            "segments": self._segments,
        }

    def feed(self, pcm16: np.ndarray) -> None:
        buf = np.concatenate([self._residual, pcm16.astype(np.int16)])
        n_windows = len(buf) // WINDOW_SAMPLES
        if n_windows == 0:
            self._residual = buf
            return

        usable = buf[: n_windows * WINDOW_SAMPLES]
        self._residual = buf[n_windows * WINDOW_SAMPLES:]

        probs = self.vad.process(usable)
        for i, prob in enumerate(probs):
            window = usable[i * WINDOW_SAMPLES:(i + 1) * WINDOW_SAMPLES]
            self._feed_window(prob, window.tobytes())

    def _feed_window(self, prob: float, window_bytes: bytes) -> None:
        self._windows += 1

        if self._suppressed:
            self._pre_roll.append(window_bytes)
            return

        if self._speaking:
            self._speech_windows += 1
            self._feed_speaking(prob, window_bytes)
        else:
            self._feed_idle(prob, window_bytes)

    def _feed_idle(self, prob: float, window_bytes: bytes) -> None:
        cfg = self.config
        if prob >= cfg.start_prob:
            self._candidate_pcm.append(window_bytes)
            self._candidate_count += 1
            if self._candidate_count >= cfg.min_speech_windows:
                pre_roll = b"".join(self._pre_roll) + b"".join(self._candidate_pcm)
                self._start_speaking(pre_roll)
        else:
            if self._candidate_pcm:
                for w in self._candidate_pcm:
                    self._pre_roll.append(w)
            self._pre_roll.append(window_bytes)
            self._candidate_count = 0
            self._candidate_pcm = []

    def _start_speaking(self, pre_roll_pcm: bytes) -> None:
        self._speaking = True
        self._pre_roll.clear()
        self._candidate_count = 0
        self._candidate_pcm = []
        self._silence_count = 0
        self._segment = [pre_roll_pcm]
        if self.on_speech_start is not None:
            self.on_speech_start(pre_roll_pcm)

    def _feed_speaking(self, prob: float, window_bytes: bytes) -> None:
        cfg = self.config
        self._segment.append(window_bytes)
        if self.on_pcm is not None:
            self.on_pcm(window_bytes)

        if prob < cfg.end_prob:
            self._silence_count += 1
        else:
            self._silence_count = 0

        if self._silence_count >= cfg.end_silence_windows:
            self._end_speech("silence")
            return

        segment_windows = len(self._segment) - 1  # 不含 pre_roll 那一筆
        if segment_windows >= cfg.max_speech_windows:
            self._end_speech("max_len")

    def _end_speech(self, reason: str) -> None:
        segment_pcm = b"".join(self._segment)
        self._segments += 1
        self._reset_to_idle()
        if self.on_speech_end is not None:
            self.on_speech_end(segment_pcm, reason)

    def _reset_to_idle(self) -> None:
        self._speaking = False
        self._pre_roll.clear()
        self._candidate_count = 0
        self._candidate_pcm = []
        self._silence_count = 0
        self._segment = []

    def set_suppressed(self, suppressed: bool) -> None:
        if suppressed:
            if self._speaking:
                segment_pcm = b"".join(self._segment)
                self._segments += 1
                self._reset_to_idle()
                if self.on_speech_end is not None:
                    self.on_speech_end(segment_pcm, "suppressed")
            else:
                self._reset_to_idle()
            self._suppressed = True
        else:
            self._suppressed = False
