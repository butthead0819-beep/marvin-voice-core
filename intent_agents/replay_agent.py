"""ReplayAgent — 重播當前歌曲 intent.

對應 2026-05-27 議題 E #2：L44「重播這一首」是 both-dense-zero 的有效 intent。

confidence 0.90，1 個 intent：replay。

mode_compatible = {"normal", "stream"}。
Gate：
  - stream_mode 必須開
  - _current_stream_info 必須存在

Handler 沿用 prev_button 的 pattern：
  - _current_stream_info 插回 stream_queue 最前面
  - vc.stop_playing() → 下一輪 picked up 同一首

不處理 radio_mode（語意模糊：radio 是隨機歌單，重播當前 file 還是重啟 fade？暫不做）。
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_bus import IntentContext

logger = logging.getLogger(__name__)


class ReplayAgent(DeclarativeIntentAgent):
    name = "replay"
    mode_compatible = frozenset({"normal", "stream"})

    def __init__(self, controller):
        self.ctrl = controller
        self._intents_cache: list[IntentSchema] | None = None

    def declare_intents(self) -> list[IntentSchema]:
        if self._intents_cache is None:
            self._intents_cache = [
                IntentSchema(
                    "replay", 0.90,
                    patterns=[
                        # 重播 / 重播這(一)?首
                        r"重播",
                        # 再(放|播|聽)一次
                        r"再\s*(放|播|聽)\s*一?次",
                        # 倒回 / 倒帶
                        r"(倒回|倒帶)",
                        # 從頭(再)?(播)?
                        r"從頭(\s*再)?(\s*播)?",
                        # replay / play again
                        r"replay",
                        r"play\s*again",
                    ],
                    reason_template="replay:{matched}",
                ),
            ]
        return self._intents_cache

    def gate(self, ctx: IntentContext) -> str | None:
        if not getattr(self.ctrl, "stream_mode", False):
            return "stream_not_active"
        if not getattr(self.ctrl, "_current_stream_info", None):
            return "no_current_song"
        return None

    def make_handler(
        self, schema: IntentSchema, slots: dict, ctx: IntentContext
    ) -> Callable[[], Awaitable[None]]:
        speaker = ctx.speaker

        async def _handler() -> None:
            try:
                self._enqueue_current()
            except Exception:
                logger.exception("[Replay] enqueue current failed")
            try:
                self._stop_current_playback()
            except Exception:
                logger.exception("[Replay] stop playback failed")
            await self._ack()
            logger.info(f"[Replay] triggered by {speaker}")

        return _handler

    def _enqueue_current(self) -> None:
        current = getattr(self.ctrl, "_current_stream_info", None)
        if current is None:
            return
        queue = getattr(self.ctrl, "stream_queue", None)
        if queue is None:
            return
        queue.insert(0, current)

    def _stop_current_playback(self) -> None:
        bot = getattr(self.ctrl, "bot", None)
        if bot is None:
            return
        for vc in getattr(bot, "voice_clients", []):
            if not getattr(vc, "is_connected", lambda: False)():
                continue
            if hasattr(vc, "stop_playing"):
                vc.stop_playing()
            elif hasattr(vc, "stop"):
                vc.stop()
            return

    async def _ack(self) -> None:
        try:
            play_tts = getattr(self.ctrl, "play_tts", None)
            if play_tts is None:
                return
            await play_tts("好，再放一次", already_in_channel=True)
        except Exception:
            logger.exception("[Replay] ack failed")
