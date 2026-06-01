"""BridgeAgent — P2 / Week 4 主菜：cross-person 橋接。

職責：A 剛講完 → 從 SpeakerTopicGraph 找在場其他人 B 過去講過相似話題的句子
     → 用 setup 句型把 A 和 B 連起來，鼓勵他們互相聊。

設計來源：docs/social_catalyst_plan.md Week 4。北極星 = 讓「人 ↔ 人」對話發生
而不是「bot ↔ 人」。

不變式：
  - 句型是 setup，不是 bot 質問（「target 也提過 X，跟 source 聊聊」式）
  - 只在 trigger="post_utterance" 觸發 bid；idle_tick 不發 callback bridge
  - exclude_speaker = A 本人；候選必須在 present_speakers 內且非 cooldown 內
  - bridge 成功後 mark_bridged 該 transcript → cooldown_days 內不再選同句
  - hot_chat 時 yield（讓人類繼續聊，不打斷）
  - bid sync-fast：只 read graph，不打 LLM / I/O

第一版用 keyword (find_similar_by_text) 而非 embedding；BridgeAgent 結構不
依賴 embedding，之後 embedding service 上線只需切 find_similar API。
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from speak_bus import SpeakBid, SpeakContext

logger = logging.getLogger(__name__)

# bridge 句型 templates（不質問 + 名字夾雜把球丟向兩人之間）
_TEMPLATES = [
    "{target} 之前也說過類似的，{source} 你們倆要不要對一下？",
    "{target} 上次也提到過耶——{source} 你聽聽看。",
    "{target} 之前也有差不多的話題，{source} 你們可以聊聊。",
]


class BridgeAgent:
    name: str = "BridgeAgent"
    # 轉場提詞同樣只適合閒置時段；stream/game/radio 由 SpeakBus 統一 gate
    mode_compatible: frozenset[str] = frozenset({"normal"})

    def __init__(
        self,
        controller,
        *,
        topic_graph,
        confidence: float = 0.65,
        cooldown_days: int = 30,
        min_overlap: float = 0.30,
        clock: Callable[[], float] = time.time,
        mood_agent=None,                # P3: heavy tier 時 yield
    ) -> None:
        self._ctrl = controller
        self._graph = topic_graph
        self._confidence = confidence
        self._cooldown_days = cooldown_days
        self._min_overlap = min_overlap
        self._clock = clock
        self._mood = mood_agent

    # ── bid contract ─────────────────────────────────────────────────────────

    async def speak_bid(self, ctx: SpeakContext) -> SpeakBid:
        """sync-fast 收：每個 0 reason distinct，方便 outcome log 追因。"""
        # 1. timing gate — 只在 post_utterance 觸發
        if ctx.trigger != "post_utterance":
            return self._dense_zero("trigger_not_post_utterance")

        # 2. 必須有最後一句
        if not ctx.last_text or not ctx.last_speaker:
            return self._dense_zero("no_last_utterance")

        # 3. ≥2 人在場才有 bridge 對象
        if len(ctx.present_speakers) < 2:
            return self._dense_zero("too_few_present")

        # 4. 熱聊時 yield（讓人類繼續講）
        if ctx.room_mood is not None and getattr(ctx.room_mood, "hot_chat", False):
            return self._dense_zero("hot_chat_yields")

        # 4.5. P3: heavy mood tier 時禮讓（房間情緒沉重 → bot 不該打擾）
        if self._mood is not None:
            try:
                tier = self._mood.get_action_tier(ctx.channel_id, silence_seconds=ctx.silence_seconds)
                if tier == "heavy":
                    return self._dense_zero("mood_heavy_yield")
            except Exception as e:
                logger.debug("[BridgeAgent] mood tier read failed: %s", e)

        # 5. 撞模式由 SpeakBus 統一 gate（mode_compatible={"normal"}）→ 此處不再重複檢查

        # 6. 從 graph 找 bridge 候選（present_speakers 內、非 last_speaker、非 cooldown）
        present_others = [s for s in ctx.present_speakers if s != ctx.last_speaker]
        if not present_others:
            return self._dense_zero("no_other_present")

        try:
            candidates = self._graph.find_similar_by_text(
                query_text=ctx.last_text,
                channel_id=ctx.channel_id,
                exclude_speaker=ctx.last_speaker,
                present_speakers=present_others,
                cooldown_days=self._cooldown_days,
            )
        except Exception as e:
            logger.warning("[BridgeAgent] graph 查詢失敗（不 bid，下次重試）: %s", e)
            return self._dense_zero("graph_error")

        if not candidates:
            return self._dense_zero("no_bridge_candidate")

        winner = candidates[0]
        if winner.get("similarity", 0.0) < self._min_overlap:
            return self._dense_zero(f"low_similarity:{winner.get('similarity', 0.0):.2f}")

        source = ctx.last_speaker

        async def _handler() -> None:
            await self._speak_bridge(source, winner)

        return SpeakBid(
            agent_name=self.name,
            confidence=self._confidence,
            handler=_handler,
            reason=(
                f"bridge:{source}→{winner['speaker']}:"
                f"sim={winner.get('similarity', 0.0):.2f}"
            ),
        )

    # ── handler ──────────────────────────────────────────────────────────────

    async def _speak_bridge(self, source: str, target_row: dict) -> None:
        """投遞 bridge：format setup 句型 → play_tts → mark_bridged。"""
        target = target_row["speaker"]
        tid = target_row["transcript_id"]
        try:
            line = _TEMPLATES[0].format(target=target, source=source)
            await self._ctrl.speak(line, proactive=True)
            # 成功投遞才 cooldown（失敗會被下次 bid 重選）
            self._graph.mark_bridged(tid)
            logger.info(
                "🌉 [Bridge] %s→%s 已投遞: %r (tid=%d)",
                source, target, line, tid,
            )
        except Exception as e:
            logger.warning("⚠️ [BridgeAgent] handler 失敗（不傳播）: %s", e)

    # ── internal ─────────────────────────────────────────────────────────────

    def _dense_zero(self, reason: str) -> SpeakBid:
        async def _noop() -> None:
            return None

        return SpeakBid(
            agent_name=self.name,
            confidence=0.0,
            handler=_noop,
            reason=reason,
        )
