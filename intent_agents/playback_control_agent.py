"""PlaybackControlAgent — Phase 1 M5: voice-driven 播放控制 + skip ack.

對應 design doc Phase 1 M5（jackhuang-main-design-20260523-131453.md）:
  - 強訊號（語音明示 skip/next/stop/pause）→ IntentBus 立刻切歌
  - 切歌 quick ack「好，換」/「停了」/「暫停」(P5 parallel 嘴 v1 簡化版)
  - skip 連 2 不同 speaker 同一 url → 自動加 cover blacklist (D3 A 方案)

Phase 1 簡化: quick ack 用 既有 paid path (Marvin tier wrapper Phase 3 才動)。
Phase 1 ack LLM 失敗 fallback 用 hardcoded 字串（D2 = A，TTS gen 一次就好；
若 prerecorded audio 尚未產，play_tts 即時 gen 也 acceptable）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_bus import IntentContext

logger = logging.getLogger(__name__)


# Quick ack text per intent（D2=A: TTS gen-on-demand；後續可 prerecord）
_ACK_TEXT = {
    "skip_track": "好，換",
    "stop_playback": "停了",
    "pause_playback": "暫停",
}

# 連續 skip 自動加黑名單 threshold (D3 A 方案)
# 註：「skip → blacklist」是 antipattern（見 memory: skip_signal_attribution.md）；
# 未來重構時把訊號送回 recommender，不是 hard ban 歌曲。
SKIP_BLACKLIST_THRESHOLD = 2  # 不同 speaker 數

# 議題 C (2026-05-27)：skip / stop / pause keyword 出現在 modal/question 之後 → chat。
# Filter 只看 prefix，避免 L19/L32 類 FP。
_CHAT_PREFIX_MARKERS = (
    # 推測 / 模糊（modal）
    "應該", "可能", "也許", "大概", "估計", "算了",
    # 疑問 / 反問
    "為什麼", "怎麼", "是不是", "有沒有", "該不該", "幹嘛",
)


class PlaybackControlAgent(DeclarativeIntentAgent):
    """語音播放控制 intent。

    三個 intent:
      skip_track     — 下一首/切歌/換歌/next/skip
      stop_playback  — 停止播放/停止/stop
      pause_playback — 暫停/pause

    mode_compatible = {"normal", "stream"}（遊戲模式不該誤觸發）。
    """

    name = "playback_control"
    mode_compatible = frozenset({"normal", "stream"})

    def __init__(self, controller):
        self.ctrl = controller
        self._intents_cache: list[IntentSchema] | None = None

    def declare_intents(self) -> list[IntentSchema]:
        if self._intents_cache is None:
            self._intents_cache = [
                IntentSchema(
                    "skip_track", 0.85,
                    patterns=[r"(下一首|切歌|換歌|跳過|next\s*song|skip)"],
                    reason_template="skip:{matched}",
                ),
                IntentSchema(
                    "stop_playback", 0.85,
                    patterns=[r"(停止播放|別播了|stop\s*play)"],
                    reason_template="stop:{matched}",
                ),
                IntentSchema(
                    "pause_playback", 0.80,
                    patterns=[r"(暫停|pause)"],
                    reason_template="pause:{matched}",
                ),
            ]
        return self._intents_cache

    def gate(self, ctx: IntentContext) -> str | None:
        # 非 stream mode 沒歌可控 → 早退
        if not getattr(self.ctrl, "stream_mode", False):
            return "stream_not_active"
        return None

    def post_match_filter(self, schema, slots, ctx) -> bool:
        """議題 C (2026-05-27)：chat marker 出現在 keyword 之前 → 拒絕。

        L19「應該下一首就是」/ L32「為什麼你下一首」這類 modal/question prefix
        的 case 被誤判 control:skip 0.95。filter 只看 prefix（matched 之前的子字串），
        避免複雜化；後續 J2 chat veto 接 prefix-less 的 case（如「下一首為什麼難聽」）。
        """
        text = ctx.query or ""
        # base class 沒把 match span 傳進來，這裡重跑 regex 找 keyword 起始位置
        import re
        pos = -1
        for pat in schema.patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                pos = m.start()
                break
        if pos <= 0:
            return True  # keyword 在開頭或找不到 → 不擋
        prefix = text[:pos]
        for marker in _CHAT_PREFIX_MARKERS:
            if marker in prefix:
                return False
        return True

    def make_handler(
        self, schema: IntentSchema, slots: dict, ctx: IntentContext
    ) -> Callable[[], Awaitable[None]]:
        intent = schema.name
        speaker = ctx.speaker

        async def _handler() -> None:
            # Phase 1 M5 P5 簡化版: ack + action 並行
            ack_text = _ACK_TEXT.get(intent, "好")
            ack_task = asyncio.create_task(self._quick_ack(ack_text))
            action_task = asyncio.create_task(self._execute(intent, speaker))
            try:
                await asyncio.gather(ack_task, action_task)
            except Exception:
                logger.exception(f"[PlaybackControl] {intent} parallel failed")

        return _handler

    async def _quick_ack(self, text: str) -> None:
        """跑 quick ack TTS。LLM/TTS 失敗 → 既有 play_tts fallback chain 自己處理。"""
        try:
            play_tts = getattr(self.ctrl, "play_tts", None)
            if play_tts is None:
                logger.warning("[PlaybackControl] ctrl.play_tts 不存在，skip ack")
                return
            await play_tts(text, already_in_channel=True)
        except Exception:
            logger.exception("[PlaybackControl] quick ack 失敗，靜默")

    async def _execute(self, intent: str, speaker: str) -> None:
        """執行 action: skip / stop / pause。skip 觸發 blacklist auto-add 邏輯。"""
        vc = self._voice_client()
        if vc is None:
            logger.warning(f"[PlaybackControl] {intent} no voice_client")
            return

        if intent == "skip_track":
            current = getattr(self.ctrl, "_current_stream_info", None)
            current_url = current.get("url") if current else None

            # blacklist auto-add 邏輯（D3 A 方案）：同一 url 被 ≥ 2 個不同 speaker skip → 黑名單
            if current_url:
                tracker = getattr(self.ctrl, "_consecutive_skips_by_url", None)
                if tracker is None:
                    self.ctrl._consecutive_skips_by_url = {}
                    tracker = self.ctrl._consecutive_skips_by_url
                spk_set = tracker.setdefault(current_url, set())
                spk_set.add(speaker)
                if len(spk_set) >= SKIP_BLACKLIST_THRESHOLD:
                    self._add_to_blacklist(current_url, current.get("title", ""), spk_set)
                    # 清掉 tracker entry（已加黑名單、不重覆加）
                    tracker.pop(current_url, None)

            try:
                if hasattr(vc, "stop_playing"):
                    vc.stop_playing()
                elif hasattr(vc, "stop"):
                    vc.stop()
                logger.info(f"[PlaybackControl] skip by {speaker} url={current_url}")
            except Exception:
                logger.exception("[PlaybackControl] skip 動作失敗")

        elif intent == "stop_playback":
            try:
                if hasattr(vc, "stop"):
                    vc.stop()
                # 同步清空 queue，避免「停」後又自動播下一首
                if hasattr(self.ctrl, "stream_queue"):
                    self.ctrl.stream_queue.clear()
                logger.info(f"[PlaybackControl] stop by {speaker}")
            except Exception:
                logger.exception("[PlaybackControl] stop 動作失敗")

        elif intent == "pause_playback":
            try:
                if hasattr(vc, "pause"):
                    vc.pause()
                if hasattr(self.ctrl, "stream_paused"):
                    self.ctrl.stream_paused = True
                logger.info(f"[PlaybackControl] pause by {speaker}")
            except Exception:
                logger.exception("[PlaybackControl] pause 動作失敗")

    def _voice_client(self):
        """從 ctrl 取當前 active voice_client；無則 None。"""
        bot = getattr(self.ctrl, "bot", None)
        if bot is None:
            return None
        for vc in getattr(bot, "voice_clients", []):
            if getattr(vc, "is_connected", lambda: False)():
                return vc
        return None

    def _add_to_blacklist(self, url: str, title: str, speakers: set[str]) -> None:
        """Skip 連 2 不同人 → 加進 cover quality blacklist。"""
        try:
            from track_quality import CoverBlacklist
            bl = getattr(self.ctrl, "_cover_blacklist", None) or CoverBlacklist.shared()
            reason = f"auto: skipped by {len(speakers)} users ({','.join(sorted(speakers))[:60]})"
            bl.add(url, reason=reason)
            self.ctrl._cover_blacklist = bl  # cache 回 ctrl
            logger.info(f"🚫 [PlaybackControl] 自動加入黑名單: {title} (url={url}) reason={reason}")
        except Exception:
            logger.exception("[PlaybackControl] 加黑名單失敗")
