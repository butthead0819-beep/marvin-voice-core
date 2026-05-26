"""VolumeAgent — 語音音量控制 intent.

對應 2026-05-27 judge outcomes 分析議題 E：L21「把調小聲一點」是 both-dense-zero
但實際是有效 intent，需要新 agent 接住。

三個 intent：
  volume_down (0.90) — 小聲/調低/音量小/volume down
  volume_up   (0.90) — 大聲/調高/音量大/volume up
  volume_mute (0.95) — 靜音/mute

模式相容：{"normal", "stream"}，game 不該誤觸發。
Gate：stream_mode 與 radio_mode 都沒開 → dense zero with "no_playback_active"
      （避免「我想小聲一點地講話」誤觸發）。

Handler：
  stream_mode → 調 controller.stream_volume（次首生效，對齊既有 UI 按鈕行為）
  radio_mode  → 調 controller.radio_volume（_radio_volume_fade_loop 即時觀察）
  mute → 設為 VOL_MIN
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_bus import IntentContext

logger = logging.getLogger(__name__)


_ACK_TEXT = {
    "volume_down": "好，調小",
    "volume_up": "好，調大",
    "volume_mute": "靜音",
}


class VolumeAgent(DeclarativeIntentAgent):
    name = "volume"
    mode_compatible = frozenset({"normal", "stream"})

    def __init__(self, controller):
        self.ctrl = controller
        self._intents_cache: list[IntentSchema] | None = None

    def declare_intents(self) -> list[IntentSchema]:
        if self._intents_cache is None:
            self._intents_cache = [
                # mute 必須排第一（regex first-match wins）。
                IntentSchema(
                    "volume_mute", 0.95,
                    patterns=[r"(靜音|mute)"],
                    reason_template="mute:{matched}",
                ),
                IntentSchema(
                    "volume_down", 0.90,
                    patterns=[
                        # 「(把)?(音量)?調?小聲(一?點)?」「音量調低/小」「volume down」
                        r"(小聲(\s*一?點)?|音量\s*(調)?(低|小)|調\s*低\s*音量|volume\s*down)",
                    ],
                    reason_template="volume_down:{matched}",
                ),
                IntentSchema(
                    "volume_up", 0.90,
                    patterns=[
                        r"(大聲(\s*一?點)?|音量\s*(調)?(高|大)|調\s*高\s*音量|volume\s*up)",
                    ],
                    reason_template="volume_up:{matched}",
                ),
            ]
        return self._intents_cache

    def gate(self, ctx: IntentContext) -> str | None:
        stream_on = getattr(self.ctrl, "stream_mode", False)
        radio_on = getattr(self.ctrl, "radio_mode", False)
        if not stream_on and not radio_on:
            return "no_playback_active"
        return None

    def make_handler(
        self, schema: IntentSchema, slots: dict, ctx: IntentContext
    ) -> Callable[[], Awaitable[None]]:
        intent = schema.name

        async def _handler() -> None:
            try:
                self._apply_volume(intent)
            except Exception:
                logger.exception(f"[Volume] {intent} apply failed")
            await self._ack(intent)

        return _handler

    def _apply_volume(self, intent: str) -> None:
        ctrl = self.ctrl
        vol_min = getattr(ctrl, "VOL_MIN", 0.01)
        vol_max = getattr(ctrl, "VOL_MAX", 1.00)
        vol_step = getattr(ctrl, "VOL_STEP", 0.05)

        # radio_mode 優先（即時 fade loop 生效）；否則 stream_mode 改 stream_volume。
        target_attr = "radio_volume" if getattr(ctrl, "radio_mode", False) else "stream_volume"
        current = getattr(ctrl, target_attr, vol_min)

        if intent == "volume_mute":
            new_val = vol_min
        elif intent == "volume_down":
            new_val = max(vol_min, round(current - vol_step, 2))
        elif intent == "volume_up":
            new_val = min(vol_max, round(current + vol_step, 2))
        else:
            return

        setattr(ctrl, target_attr, new_val)
        logger.info(f"[Volume] {intent} → {target_attr}={new_val:.2f}")

    async def _ack(self, intent: str) -> None:
        try:
            play_tts = getattr(self.ctrl, "play_tts", None)
            if play_tts is None:
                return
            await play_tts(_ACK_TEXT.get(intent, "好"), already_in_channel=True)
        except Exception:
            logger.exception("[Volume] ack failed")
