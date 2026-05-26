"""ProactiveTopicAgent — 第一個會 bid 的 SpeakAgent（social-catalyst 收尾）。

把 slow_system_loop 內「靜默 X 秒主動發起話題」這條獨立 timer 路徑移到 SpeakBus。
原邏輯保留在 VoiceController.trigger_proactive_topic（含 topic 選題、改寫、TTS），
本檔只做 bid 階段的 sync-fast gate。

Bid 契約（per docs/social_catalyst_plan.md + memory speakbus_and_survival）：
  - speak_bid sync-fast（≤5ms）：禁 LLM / I/O / subprocess
  - handler 才做重活（fetch topics、LLM 改寫、TTS）

Bid 條件（全 AND）：
  - silence ≥ proactive_silence_threshold（controller 動態調整 240/300/600）
  - 距離上次 last_proactive_time ≥ min_gap_since_last_s（預設 1800）
  - 有在場玩家 + active_text_channel
  - 不在 radio_mode / stream_mode / game
"""
from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable

from speak_bus import SpeakBid, SpeakContext

logger = logging.getLogger(__name__)


class ProactiveTopicAgent:
    name: str = "ProactiveTopicAgent"

    def __init__(
        self,
        controller,
        *,
        confidence: float = 0.6,
        min_gap_since_last_s: float = 1800.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ctrl = controller
        self._confidence = confidence
        self._min_gap = min_gap_since_last_s
        self._clock = clock

    async def speak_bid(self, ctx: SpeakContext) -> SpeakBid | None:
        # sync-fast gate — 全部都是 attribute read，沒 I/O
        c = self._ctrl

        # 1. 靜默是否夠久
        threshold = getattr(c, "proactive_silence_threshold", 300.0)
        if ctx.silence_seconds < threshold:
            return None

        # 2. 距上次主動不夠久
        last_proactive = getattr(c, "last_proactive_time", 0.0) or 0.0
        if self._clock() - last_proactive < self._min_gap:
            return None

        # 3. 沒在場玩家 / 沒文字頻道 → 自言自語沒意義
        if not ctx.present_speakers:
            return None
        if not getattr(c, "active_text_channel", None):
            return None

        # 4. 撞模式：電台播放 / 音樂串流 / 遊戲進行中
        if getattr(c, "radio_mode", False):
            return None
        if getattr(c, "stream_mode", False):
            return None
        if getattr(getattr(getattr(c, "bot", None), "router", None), "current_game", None):
            return None

        async def _handler() -> None:
            try:
                await c.trigger_proactive_topic()
            except Exception:
                logger.exception("[ProactiveTopicAgent] handler raised")

        return SpeakBid(
            agent_name=self.name,
            confidence=self._confidence,
            handler=_handler,
            reason=f"social_gap:{int(ctx.silence_seconds)}s",
        )
