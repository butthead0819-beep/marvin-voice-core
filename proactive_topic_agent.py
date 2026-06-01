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
    # 主動拋話題在 stream/game/radio 都不適合（音樂、遊戲時別硬塞閒聊），bus 統一 gate
    mode_compatible: frozenset[str] = frozenset({"normal"})

    def __init__(
        self,
        controller,
        *,
        confidence: float = 0.6,
        min_gap_since_last_s: float = 600.0,   # P0: 1800 → 600（北極星復活）
        clock: Callable[[], float] = time.time,
        topic_graph=None,                       # P0: SpeakerTopicGraph 接入，bid 時讀 recent
        mood_agent=None,                        # P3: heavy tier 時 yield
    ) -> None:
        self._ctrl = controller
        self._confidence = confidence
        self._min_gap = min_gap_since_last_s
        self._clock = clock
        self._graph = topic_graph
        self._mood = mood_agent

    async def speak_bid(self, ctx: SpeakContext) -> SpeakBid | None:
        # sync-fast gate — 全部都是 attribute read，沒 I/O
        c = self._ctrl

        # 0. P3: heavy mood tier 時禮讓（低落+低溫+長靜默 = 房間真的不適合 bot 插話）
        if self._mood is not None:
            try:
                tier = self._mood.get_action_tier(ctx.channel_id, silence_seconds=ctx.silence_seconds)
                if tier == "heavy":
                    return None  # 完全不 bid（沿用既有 None 回傳契約）
            except Exception as e:
                logger.debug("[ProactiveTopicAgent] mood tier read failed: %s", e)

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

        # 4. 撞模式由 SpeakBus 統一 gate（mode_compatible={"normal"}）→ 此處不再重複檢查

        async def _handler() -> None:
            try:
                await c.trigger_proactive_topic()
            except Exception:
                logger.exception("[ProactiveTopicAgent] handler raised")

        # P0: 讀 SpeakerTopicGraph 把資料引入 reason（讓死水資料開始流動）
        reason = f"social_gap:{int(ctx.silence_seconds)}s"
        if self._graph is not None:
            try:
                rows = self._graph.recent(ctx.channel_id, n=10)
                if rows:
                    speakers = len({r["speaker"] for r in rows})
                    sample = rows[0]["text"][:20] if rows else ""
                    reason = f"social_gap:{int(ctx.silence_seconds)}s graph:speakers:{speakers}:{sample}"
            except Exception as e:
                logger.debug("[ProactiveTopicAgent] graph read failed (degrading): %s", e)

        return SpeakBid(
            agent_name=self.name,
            confidence=self._confidence,
            handler=_handler,
            reason=reason,
        )
