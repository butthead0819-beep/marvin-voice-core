"""MemoryCallbackAgent — SpeakBus 第二個 SpeakAgent（plan-eng-review 2026-05-26 拍定）。

把「Jack 三天前說要試 X、現在 Jack 又提到 X」的主題關聯 callback 包成 SpeakBus bid。

設計來源：~/.gstack/projects/butthead0819-beep-marvin-voice-core/
         jackhuang-main-design-MemoryCallbackAgent-20260526-130249.md

關鍵約束（per plan-eng-review）：
  - 走既有 5s idle tick（trigger="idle_tick"），不加 post_utterance trigger
  - agent 自讀 controller.bot.engine.conv_buffer.history 拉近 N 秒 utterance
  - 用 suki_memory.peek_all_shareable_callbacks（D8 新 API，回 list）
  - 用 char-overlap 沿 speaker_topic_graph.py:227 pattern（不引 jieba）
  - 每條 dense reason distinct（CLAUDE.md SpeakBus 規則承自 IntentBus）
  - bid sync-fast ≤ 5ms：禁 LLM / I/O / subprocess

Feature flag：SPEAK_MEMORY_CALLBACK=true 才啟用，預設 OFF（merge 不改變行為）。

T3 範圍：skeleton + char overlap + bid 邏輯。Handler 細節（playback_lock + TTS + consume）
        是佔位 stub，T4 完成。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable

from speak_bus import SpeakBid, SpeakContext

logger = logging.getLogger(__name__)


# ── tokenization (沿 speaker_topic_graph.py:249-258 既有 pattern) ─────────────

_OVERLAP_PUNCT = frozenset(" \t\n，。！？,.!?\"'()（）「」、—-_:;；：")

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _char_set(text: str) -> set[str]:
    """unique-char set 去標點。中英統一 lowercase 再剝。"""
    return set(text.lower()) - _OVERLAP_PUNCT


def _char_overlap(q: set[str], t: set[str]) -> float:
    """overlap ratio = |q ∩ t| / |q|。q 空回 0.0。"""
    if not q or not t:
        return 0.0
    return len(q & t) / len(q)


def _is_enabled() -> bool:
    return os.environ.get("SPEAK_MEMORY_CALLBACK", "").strip().lower() in _TRUE_VALUES


# ── agent ─────────────────────────────────────────────────────────────────────


class MemoryCallbackAgent:
    name: str = "MemoryCallbackAgent"

    def __init__(
        self,
        controller,
        *,
        confidence: float = 0.7,
        cooldown_s: float = 1800.0,
        overlap_threshold: float = 0.35,
        recent_utt_window_s: float = 10.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ctrl = controller
        self._confidence = confidence
        self._cooldown_s = cooldown_s
        self._overlap_threshold = overlap_threshold
        self._recent_utt_window_s = recent_utt_window_s
        self._clock = clock
        # key = (speaker, item_ts) — 不用 hash(text) 避免相同文字異筆混淆
        self._bid_history: dict[tuple[str, float], float] = {}

    # ── helpers ──────────────────────────────────────────────────────────────

    def _recent_utterance(self) -> dict | None:
        """從 conv_buffer.history 反向找近 recent_utt_window_s 秒內最後一筆 STT。"""
        history = getattr(getattr(getattr(self._ctrl, "bot", None), "engine", None), "conv_buffer", None)
        if history is None:
            return None
        entries = getattr(history, "history", None)
        if not entries:
            return None
        cutoff = self._clock() - self._recent_utt_window_s
        for entry in reversed(entries):
            if entry.get("timestamp", 0.0) >= cutoff:
                return entry
        return None

    def _in_cooldown(self, speaker: str, item: dict) -> bool:
        key = (speaker, float(item.get("ts", 0.0)))
        last = self._bid_history.get(key)
        if last is None:
            return False
        return (self._clock() - last) < self._cooldown_s

    def _mark_bid(self, speaker: str, item: dict) -> None:
        self._bid_history[(speaker, float(item.get("ts", 0.0)))] = self._clock()

    # ── bid contract ─────────────────────────────────────────────────────────

    async def speak_bid(self, ctx: SpeakContext) -> SpeakBid:
        """sync-fast：每條 dense reason distinct，方便 outcome log 追因。"""
        # 1. feature flag
        if not _is_enabled():
            return self._dense(0.0, "feature_off")

        # 2. 撞模式：音樂串流中不主動 callback（看齊 Proactive/Bridge）。
        # 想在 stream 期間發話時，改 handler 走 vc.speak() 並移除此 gate。
        if getattr(self._ctrl, "stream_mode", False):
            return self._dense(0.0, "stream_mode")

        # 3. 沒玩家在場
        if not ctx.present_speakers:
            return self._dense(0.0, "no_present")

        mem = getattr(getattr(getattr(self._ctrl, "bot", None), "router", None), "memory", None)
        if mem is None:
            return self._dense(0.0, "no_memory")

        # 3. 全 muted（每個 present speaker 都 mute → all_muted；否則繼續）
        unmuted = [spk for spk in ctx.present_speakers if not mem.is_callbacks_muted(spk)]
        if not unmuted:
            return self._dense(0.0, "all_muted")

        # 4. 至少一個 present speaker 有 shareable callback
        cb_pairs: list[tuple[str, dict]] = []
        for spk in unmuted:
            for item in mem.peek_all_shareable_callbacks(spk):
                cb_pairs.append((spk, item))
        if not cb_pairs:
            return self._dense(0.0, "no_callbacks")

        # 5. 近 N 秒有 utterance
        utt = self._recent_utterance()
        if utt is None:
            return self._dense(0.0, "no_recent_utt")

        # 6. char overlap 跑全部命中、取最新 commitment_ts 者
        utt_chars = _char_set(utt.get("text", ""))
        if not utt_chars:
            return self._dense(0.0, "no_recent_utt")

        hits: list[tuple[str, dict, float]] = []  # (speaker, item, overlap)
        for spk, item in cb_pairs:
            if self._in_cooldown(spk, item):
                continue
            cb_chars = _char_set(item.get("text", ""))
            overlap = _char_overlap(utt_chars, cb_chars)
            if overlap >= self._overlap_threshold:
                hits.append((spk, item, overlap))

        if not hits:
            # 區分 cooldown vs no_topic_overlap：若全部都 in cooldown 則 reason=cooldown
            all_in_cd = all(
                self._in_cooldown(spk, item) for spk, item in cb_pairs
            )
            return self._dense(0.0, "cooldown" if all_in_cd else "no_topic_overlap")

        # 多筆命中 → 取最新 commitment_ts
        hits.sort(key=lambda h: h[1].get("ts", 0.0), reverse=True)
        winner_spk, winner_item, winner_overlap = hits[0]
        self._mark_bid(winner_spk, winner_item)

        async def _handler() -> None:
            # T4 範圍：playback_lock + format_topic_callback_line + TTS + consume
            await self._speak_callback(winner_spk, winner_item)

        reason = f"topic_overlap:{winner_overlap:.2f}:{winner_item.get('text', '')[:40]}"
        return SpeakBid(
            agent_name=self.name,
            confidence=self._confidence,
            handler=_handler,
            reason=reason,
        )

    # ── handler (T4) ─────────────────────────────────────────────────────────

    async def _speak_callback(self, speaker: str, item: dict) -> None:
        """主題關聯 callback 投遞：format → truncate gate → play_tts → consume。

        沿 cogs/voice_controller._maybe_speak_join_callback (T3 join 路徑) 同 pattern：
          - 任何錯誤都吞掉，不傳播（SpeakBus tick loop 也 catch 但 handler 自護更穩）
          - TTS 成功才 consume（idempotent 重投：失敗 → 下次再 bid）
        """
        try:
            from callback_delivery import format_topic_callback_line
            from tts_length_policy import truncate_for_tts

            line = format_topic_callback_line(item.get("text", ""))
            if not line:
                return

            mem = self._ctrl.bot.router.memory
            gated_line, was_cut = truncate_for_tts(
                line, "callback", self._ctrl.bot.tts_engine.get_estimated_duration
            )
            if was_cut:
                logger.info(
                    "🚦 [TTS Gate] memory callback 超 7s 截斷: %r → %r", line, gated_line
                )
                line = gated_line

            stt_logger = getattr(self._ctrl, "stt_logger", None)
            if stt_logger is not None:
                stt_logger.info(f"[BOT主題callback→{speaker}] {line}")

            await self._ctrl.speak(line, proactive=True)
            # TTS 成功 → consume（idempotent；T3 race 二次 consume 為 no-op）
            mem.consume_callback(speaker, item)
        except Exception as e:
            logger.warning(
                "⚠️ [MemoryCallbackAgent] 投遞失敗（不傳播，下次重投）: %s", e
            )

    # ── internal ─────────────────────────────────────────────────────────────

    def _dense(self, confidence: float, reason: str) -> SpeakBid:
        """Dense 0.0 bid（含 no-op handler，per SpeakBid 必填 handler field）。"""

        async def _noop() -> None:
            return None

        return SpeakBid(
            agent_name=self.name,
            confidence=confidence,
            handler=_noop,
            reason=reason,
        )
