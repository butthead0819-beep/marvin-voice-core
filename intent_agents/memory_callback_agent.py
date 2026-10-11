"""MemoryCallbackAgent — SpeakBus 第二個 SpeakAgent。

事件記憶 Phase 2（docs/designs/event-memory.md）：事件日期過後，當事人在場時
主動追問「後來怎麼樣」。取代舊版 callback_queue 主題關聯 callback（整套已拔除，
2026-10-11 使用者拍板）。候選來自 event_memory.EventStore.list_followup_candidates，
過濾走 event_memory.pick_followups（owner 在場 + 隱私濾網 + 時間窗），每個事件只問
一次（event_followups 表標記，mark_followed_up）。

播歌中（stream mode）的事件追問改由 DJ callback mode 講
（cogs/music_cog_dj_lyrics.py::_present_callbacks）；這裡只在 normal mode 生效。

Feature flag：SPEAK_MEMORY_CALLBACK=true 才啟用，預設 OFF（merge 不改變行為）。
"""
from __future__ import annotations

import logging
import os
import time
import asyncio
from typing import Callable

from event_memory import EventStore, pick_followups, render_followup_line
from speak_bus import SpeakBid, SpeakContext

logger = logging.getLogger(__name__)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _is_enabled() -> bool:
    return os.environ.get("SPEAK_MEMORY_CALLBACK", "").strip().lower() in _TRUE_VALUES


class MemoryCallbackAgent:
    name: str = "MemoryCallbackAgent"
    # 播歌中（stream）的事件追問交給 DJ callback mode；這裡只在 normal mode 生效。
    mode_compatible: frozenset[str] = frozenset({"normal"})

    def __init__(
        self,
        controller,
        *,
        store=None,
        confidence: float = 0.7,
        cooldown_s: float = 1800.0,
        refresh_s: float = 300.0,
        music_control_cooldown_s: float = 30.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ctrl = controller
        self._store = store
        self._confidence = confidence
        self._cooldown_s = cooldown_s
        self._refresh_s = refresh_s
        self._music_control_cooldown_s = music_control_cooldown_s
        self._clock = clock
        self._candidates: list[dict] = []
        self._cache_ts = 0.0
        self._refresh_task: asyncio.Task | None = None
        self._last_spoken = 0.0

    def _guild_id(self) -> int:
        return int(os.environ.get("GUILD_ID") or "0")

    async def _refresh(self) -> None:
        try:
            store = self._store or EventStore()
            self._store = store
            rows = await asyncio.to_thread(store.list_followup_candidates, self._guild_id())
            self._candidates = rows
        except Exception as e:
            logger.warning(f"⚠️ [MemoryCallbackAgent] 候選刷新失敗: {e}")
        finally:
            self._cache_ts = self._clock()

    # ── bid contract ─────────────────────────────────────────────────────────

    async def speak_bid(self, ctx: SpeakContext) -> SpeakBid:
        if not _is_enabled():
            return self._dense(0.0, "feature_off")

        if not ctx.present_speakers:
            return self._dense(0.0, "no_present")

        last_mc = getattr(self._ctrl, "last_music_control_time", 0.0)
        if not isinstance(last_mc, (int, float)):
            last_mc = 0.0
        if last_mc and (self._clock() - last_mc) < self._music_control_cooldown_s:
            return self._dense(0.0, "music_control_cooldown")

        clock = self._clock()
        if self._last_spoken and (clock - self._last_spoken) < self._cooldown_s:
            return self._dense(0.0, "cooldown")

        if clock - self._cache_ts >= self._refresh_s:
            if self._refresh_task is None or self._refresh_task.done():
                self._refresh_task = asyncio.create_task(self._refresh())

        picks = pick_followups(self._candidates, ctx.present_speakers, clock)
        if not picks:
            return self._dense(0.0, "no_due_event")

        ev = picks[0]

        async def _handler() -> None:
            await self._speak_followup(ev)

        reason = f"event_followup:{ev['id']}:{ev['owner']}:{ev['activity']}"
        return SpeakBid(
            agent_name=self.name,
            confidence=self._confidence,
            handler=_handler,
            reason=reason,
        )

    # ── handler ──────────────────────────────────────────────────────────────

    async def _speak_followup(self, ev: dict) -> None:
        try:
            from tts_length_policy import truncate_for_tts
            from tts_speak_policy import SpeakKind

            line = render_followup_line(ev)
            gated_line, was_cut = truncate_for_tts(
                line, "callback", self._ctrl.bot.tts_engine.get_estimated_duration
            )
            if was_cut:
                logger.info(f"🚦 [TTS Gate] 事件追問超 7s 截斷: '{line}' → '{gated_line}'")
                line = gated_line

            stt_logger = getattr(self._ctrl, "stt_logger", None)
            if stt_logger is not None:
                stt_logger.info(f"[BOT事件追問→{ev['owner']}] {line}")

            await self._ctrl.speak(line, proactive=True, allow_dual=False, kind=SpeakKind.MEMORY_CALLBACK)

            store = self._store or EventStore()
            self._store = store
            await asyncio.to_thread(store.mark_followed_up, ev["id"], self._clock())
            self._candidates = [c for c in self._candidates if c["id"] != ev["id"]]
            self._last_spoken = self._clock()
        except Exception as e:
            logger.warning(f"⚠️ [MemoryCallbackAgent] 投遞失敗（不傳播，下次重投）: {e}")

    # ── internal ─────────────────────────────────────────────────────────────

    def _dense(self, confidence: float, reason: str) -> SpeakBid:
        async def _noop() -> None:
            return None

        return SpeakBid(
            agent_name=self.name,
            confidence=confidence,
            handler=_noop,
            reason=reason,
        )
