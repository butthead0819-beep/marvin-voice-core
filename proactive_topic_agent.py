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
  - 在場玩家 ≥2（催化人↔人對話需有對象；單人房接話率僅 ~4%）+ active_text_channel
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
        stale_after_days: int = 3,             # proactive_topics 過期門檻（daily review 維護）
    ) -> None:
        self._ctrl = controller
        self._confidence = confidence
        self._min_gap = min_gap_since_last_s
        self._clock = clock
        self._graph = topic_graph
        self._stale_after_days = stale_after_days
        self._mood = mood_agent

    def _topics_stale(self) -> bool:
        """suki_memory._meta.review_date 比門檻舊 → True（topics 凍結，不該再講）。

        Fail open：讀不到 / 無法解析 review_date → False（不擋），避免讀取問題誤殺
        整個主動話題。確認過期才擋。sync-fast（in-memory dict read）。
        """
        try:
            mem = self._ctrl.bot.router.memory
            rd = mem.get_meta("review_date")
            if not rd:
                return False
            from datetime import datetime
            review_day = datetime.strptime(rd, "%Y-%m-%d").date()
            today = datetime.fromtimestamp(self._clock()).date()
            return (today - review_day).days > self._stale_after_days
        except Exception:
            return False

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

        # 3. 在場玩家 < 2 / 沒文字頻道 → 自言自語沒意義。
        #    需 ≥2 人才 bid：對單一（多半 AFK）聽眾主動拋話題接話率僅 ~4%
        #    （records/speak_outcomes.jsonl：627/645 場 present_speakers=1）。
        #    催化「人↔人」對話本來就需要 ≥2 個對象。
        if len(ctx.present_speakers or ()) < 2:
            return None
        if not getattr(c, "active_text_channel", None):
            return None

        # 3.5. proactive_topics 過期不講：daily review 卡住時 topics 凍結，一直翻舊
        # 話題浪費 LLM 又零互動（2026-06-02：老話題觸發 13 次效益全 0）。讓即時的冷場
        # TopicGenerator 接手。fail open：讀不到 review_date 就不擋。
        if self._topics_stale():
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
