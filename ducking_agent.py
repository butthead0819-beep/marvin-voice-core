"""DuckingAgent — week 2 of social-catalyst plan.

熱聊偵測 + 壓制器。**不繼承 SpeakAgent、不發話**——只在偵測到「兩個 speaker
在 15s 內交替發話 ≥3 次」時，呼叫 SpeakBus.set_global_multiplier(0.2, ttl_s=30)
壓制其他主動發話 agent。

合約（per docs/social_catalyst_plan.md）：
- 不該自己發話（無 speak_bid method）
- TTS fade-out 必須走既有 playback_lock 鏈（v2 再做，本檔不碰）
- wake 閾值調整僅透過 ctx hint（v2 再做）

設計：pure detection 核心（吃 buffer + clock，回 bool），IO 動作（call bus）在 thin shell。
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Callable

logger = logging.getLogger(__name__)


class DuckingAgent:
    name: str = "DuckingAgent"

    def __init__(
        self,
        speak_bus,
        *,
        window_s: float = 15.0,
        min_turns: int = 3,
        multiplier: float = 0.2,
        suppress_ttl_s: float = 30.0,
        suppress_cooldown_s: float = 5.0,
        buffer_size: int = 32,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._bus = speak_bus
        self._window_s = window_s
        self._min_turns = min_turns
        self._multiplier = multiplier
        self._suppress_ttl_s = suppress_ttl_s
        self._suppress_cooldown_s = suppress_cooldown_s
        self._buffer: deque[tuple[str, float]] = deque(maxlen=buffer_size)
        self._clock = clock
        self._last_suppress_at: float = float("-inf")  # 確保第一次觸發不被 cooldown 卡

    # ── public API ───────────────────────────────────────────────────────────

    def on_utterance(self, speaker: str, ts: float | None = None) -> None:
        """每筆 STT 結果都呼一次。pure-ish：只動 buffer，命中時走 thin shell 進 bus。"""
        if not speaker:
            return
        self._buffer.append((speaker, ts if ts is not None else self._clock()))
        if self._detect_hot_chat():
            self._activate_suppression()

    # ── detection (pure) ─────────────────────────────────────────────────────

    def _detect_hot_chat(self) -> bool:
        """plan 偽碼：window 內 ≥ min_turns，last min_turns 個只有 2 個 speaker，且最後兩個不同。"""
        if not self._buffer:
            return False
        latest_ts = self._buffer[-1][1]
        recent = [(s, t) for s, t in self._buffer if latest_ts - t <= self._window_s]
        if len(recent) < self._min_turns:
            return False
        last = recent[-self._min_turns:]
        speakers = [s for s, _ in last]
        if len(set(speakers)) != 2:
            return False
        if speakers[-1] == speakers[-2]:
            return False
        return True

    # ── action (thin IO shell) ───────────────────────────────────────────────

    def _activate_suppression(self) -> None:
        now = self._clock()
        if now - self._last_suppress_at < self._suppress_cooldown_s:
            return
        self._last_suppress_at = now
        self._bus.set_global_multiplier(self._multiplier, ttl_s=self._suppress_ttl_s)
        last_pair = sorted({s for s, _ in list(self._buffer)[-self._min_turns:]})
        logger.info(
            f"🦆 [Ducking] hot_chat 觸發 {last_pair} → multiplier={self._multiplier} "
            f"for {self._suppress_ttl_s}s"
        )
