"""SpontaneousManzaiAgent — SpeakBus agent：Marvin 自發雙人漫才（不依賴 openclaw）。

跟 ProactiveTopicAgent 同層（proactive 主動發話），但產出的是 Marvin+Marmo 雙段對白
（generate_dual_dialogue pattern="marvin_lead" → Marvin 拋題、Marmo 打斷），走
play_dual_dialogue 序列播。不需要 marmo_server 收到任何外部內容。

設計取捨：
- manzai 重（1 次 LLM 生雙段 + 2 段 TTS，~10-20s），歷史上高頻會搶爆 LLM bus 害喚醒
  回應 429 → 硬 cooldown（預設 30min）+ 靜默門檻 + 低 confidence（讓 ProactiveTopic/
  Bridge 先發，漫才只在冷場補位）。
- 取材自 recent_utterances（吐槽剛剛的對話），不是憑空。
- env SPONTANEOUS_MANZAI 預設 OFF（謹慎上線；set true 開，hot-flippable）。

bid sync-fast：只 attr read + os.getenv，禁 LLM / I/O / subprocess；重活在 handler。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Awaitable, Callable

from speak_bus import SpeakBid, SpeakContext

logger = logging.getLogger(__name__)


class SpontaneousManzaiAgent:
    name: str = "SpontaneousManzaiAgent"
    # 漫才在 stream(音樂)/game 都不適合插入；bus 統一 gate
    mode_compatible: frozenset[str] = frozenset({"normal"})

    def __init__(
        self,
        controller,
        *,
        llm_fn,
        confidence: float = 0.4,                 # < ProactiveTopic 0.6：冷場補位、不搶主動話題
        silence_threshold_s: float = 120.0,      # 房間冷場才插漫才
        min_gap_since_last_s: float = 1800.0,    # 距上次漫才 ≥30min（重活、防搶爆 bus）
        min_present: int = 1,                    # 至少要有觀眾
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ctrl = controller
        self._llm_fn = llm_fn
        self._confidence = confidence
        self._silence_threshold = silence_threshold_s
        self._min_gap = min_gap_since_last_s
        self._min_present = min_present
        self._clock = clock

    @staticmethod
    def _enabled() -> bool:
        return os.getenv("SPONTANEOUS_MANZAI", "").strip().lower() in ("1", "true", "yes")

    async def speak_bid(self, ctx: SpeakContext) -> SpeakBid | None:
        # 0. env gate（預設 OFF）
        if not self._enabled():
            return None
        # 1. 靜默夠久（冷場）
        if ctx.silence_seconds < self._silence_threshold:
            return None
        # 2. 距上次漫才夠久
        last = getattr(self._ctrl, "_last_manzai_time", 0.0) or 0.0
        if self._clock() - last < self._min_gap:
            return None
        # 3. 要有觀眾
        if len(ctx.present_speakers or ()) < self._min_present:
            return None
        # 4. 要有近期對話可吐槽（取材）
        if not ctx.recent_utterances:
            return None

        async def _handler() -> None:
            await self._perform(ctx)

        return SpeakBid(
            agent_name=self.name,
            confidence=self._confidence,
            handler=_handler,
            reason=f"manzai:silence={int(ctx.silence_seconds)}s:utts={len(ctx.recent_utterances)}",
        )

    async def _perform(self, ctx: SpeakContext) -> None:
        # 先記 cooldown（即使生成失敗也別連續重試搶池）
        self._ctrl._last_manzai_time = self._clock()

        content = "\n".join(
            f"{u.get('speaker', '?')}: {u.get('text', '')}"
            for u in (ctx.recent_utterances or [])[-5:]
        ).strip()
        if not content:
            return

        # lazy import：避免 agent 載入就拉 services 依賴鏈
        from services.dialogue_generation import generate_dual_dialogue
        try:
            segments = await generate_dual_dialogue(
                content_text=content, llm_fn=self._llm_fn, pattern="marvin_lead",
            )
        except Exception:
            logger.exception("[SpontaneousManzai] generate_dual_dialogue raised")
            segments = None

        if not segments:
            logger.info("[SpontaneousManzai] 生成失敗/空 → 跳過（spontaneous 不 fallback solo）")
            return

        try:
            await self._ctrl.play_dual_dialogue(segments)
        except Exception:
            logger.exception("[SpontaneousManzai] play_dual_dialogue raised")
