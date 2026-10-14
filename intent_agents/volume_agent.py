"""VolumeAgent — 語音音量控制 intent.

對應 2026-05-27 judge outcomes 分析議題 E：L21「把調小聲一點」是 both-dense-zero
但實際是有效 intent，需要新 agent 接住。

六個 intent：
  volume_down (0.90) — 小聲/調低/音量小/volume down
  volume_up   (0.90) — 大聲/調高/音量大/volume up
  volume_mute (0.95) — 靜音/mute
  balance_tts_up   (0.92) — 「你講話/語音/人聲」大聲一點 → 語音:音樂平衡（非總音量）
  balance_tts_down (0.92) — 「你講話/語音/人聲」小聲一點 → 語音:音樂平衡（非總音量）
  balance_reset    (0.92) — 平衡/各半 → 語音:音樂恢復 50:50

模式相容：{"normal", "stream"}，game 不該誤觸發。
Gate：stream_mode 與 radio_mode 都沒開 → dense zero with "no_playback_active"
      （避免「我想小聲一點地講話」誤觸發）。

Handler：
  stream_mode → 調 controller.stream_volume（本地混音台每 100ms 輪詢套用，見
                voice_controller_playback.py _mixer_play_music，無需熱切換）。步進 25%。
  radio_mode  → 調 controller.radio_volume（_radio_volume_fade_loop 即時觀察）
  mute → 設為 VOL_MIN

  出口音量統一管音樂+TTS：mixer.set_volume() 是 Discord 出口層，調 stream_volume/
  radio_volume 時直接同步設成同一個值（不再對 TTS 另外做響度補償，2026-10 統一
  輸出增益架構後 TTS 相對增益固定 1.0）。

Ack：文字頻道 ch.send() + play_tts 雙保險。play_tts 常被 [TTS Silence Gate]
     （使用者仍在講話）靜默吞掉，音量調整因此完全沒有回饋、使用者以為指令沒被
     執行（2026-09-04 實測 case）；文字頻道不受這個 gate 影響，比照 pause/
     stop/skip 的 ch.send() 保底模式（2026-09-05 補上）。
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_bus import IntentContext

logger = logging.getLogger(__name__)


def sync_tts_gain(ctrl, value: float) -> None:
    """把 mixer 的 Discord 出口音量設成新的音樂音量值。

    語音指令（VolumeAgent）與控制面板按鈕（voice_views.py）共用同一份邏輯。出口層
    統一管音樂+TTS（兩者在內容層已各自到參考準位，TTS 相對增益固定 1.0），調音量
    不用再對 TTS 另外做響度補償。
    """
    mixer = getattr(ctrl, "_mixer", None)
    if mixer is None:
        return
    try:
        mixer.set_volume(float(value))
        logger.info(f"[Volume] Discord 出口音量 → {float(value):.2f}（音樂+TTS 同一出口）")
    except Exception:
        logger.exception("[Volume] tts gain sync failed")


# 語音步進 25%（按鈕 UI PlayControlView.VOL_STEP=0.05 較細，2026-06-04 起兩者不同）。
# 2026-08-25：使用者反映「小聲一點」原本 10% 降幅無感，改大幅遞減。
VOICE_VOL_STEP = 0.25

# 平衡（語音:音樂）語音步進 20%；夾在 PlayControlView.BAL_MIN/BAL_MAX 同範圍。
VOICE_BAL_STEP = 0.20
BAL_MIN = 0.10
BAL_MAX = 0.90
_VOICE_SUBJ = r"(?:(?:你|馬文)的?(?:講話|說話|聲音)|語音|人聲)"

_ACK_TEXT = {
    "volume_down": "好，調小",
    "volume_up": "好，調大",
    "volume_mute": "靜音",
    "balance_tts_up": "好，語音調大",
    "balance_tts_down": "好，語音調小",
    "balance_reset": "好，語音音樂各半",
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
                    manifest_description=(
                        "使用者要把聲音完全靜音（一點聲音都不要）。不是調小聲、"
                        "不是暫停。"
                    ),
                ),
                IntentSchema(
                    "balance_tts_up", 0.92,
                    patterns=[
                        rf"{_VOICE_SUBJ}\s*(?:再|調)?\s*(?:大聲|大一?點|大)",
                        r"聽不(?:到|清楚?)你(?:講話|說話|的聲音)?",
                        r"音樂.{0,3}(?:蓋過|壓過|蓋掉)你",
                    ],
                    reason_template="balance_tts_up:{matched}",
                    manifest_description=(
                        "使用者要 Marvin 講話（語音/人聲）相對音樂大聲一點，調的是"
                        "語音:音樂平衡，不是總音量。"
                    ),
                ),
                IntentSchema(
                    "balance_tts_down", 0.92,
                    patterns=[
                        rf"{_VOICE_SUBJ}\s*(?:再|調)?\s*(?:小聲|小一?點|小)",
                    ],
                    reason_template="balance_tts_down:{matched}",
                    manifest_description=(
                        "使用者要 Marvin 講話（語音/人聲）相對音樂小聲一點，調的是"
                        "語音:音樂平衡，不是總音量。"
                    ),
                ),
                IntentSchema(
                    "balance_reset", 0.92,
                    patterns=[
                        r"(?:平衡|各半|一半一半)",
                    ],
                    reason_template="balance_reset:{matched}",
                    manifest_description=(
                        "使用者要把語音和音樂的音量比例恢復成一半一半。"
                    ),
                ),
                IntentSchema(
                    "volume_down", 0.90,
                    patterns=[
                        # 「(把)?(音量)?調?小聲(一?點)?」「音量調低/小」「調低(的)?音量」「volume down」
                        # 「的」容錯：口語常見「調低的音量」而非「調低音量」（8/18 gap 實測：
                        # 「總動調高的音量」被嚴格 \s* 卡掉，見同款 volume_up 修正）。
                        r"(小聲(\s*一?點)?|音量\s*(調)?(低|小)|調\s*低\s*(的)?\s*音量|volume\s*down)",
                    ],
                    reason_template="volume_down:{matched}",
                    manifest_description=(
                        "使用者要把現在播放的音量調小聲一點。不是要暫停、不是要換歌、"
                        "不是要靜音。"
                    ),
                ),
                IntentSchema(
                    "volume_up", 0.90,
                    patterns=[
                        r"(大聲(\s*一?點)?|音量\s*(調)?(高|大)|調\s*高\s*(的)?\s*音量|volume\s*up)",
                    ],
                    reason_template="volume_up:{matched}",
                    manifest_description=(
                        "使用者要把現在播放的音量調大聲一點。"
                    ),
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
            # 本地混音台輪詢 stream_volume/radio_volume 套用新值，兩者都不需要熱切換。
            await self._ack(intent)

        return _handler

    def _apply_volume(self, intent: str) -> str | None:
        """套用音量變更，回傳實際被調的屬性名（"stream_volume" / "radio_volume" /
        "tts_balance"）；無變更回 None。"""
        ctrl = self.ctrl

        if intent in ("balance_tts_up", "balance_tts_down", "balance_reset"):
            cur = getattr(ctrl, "tts_balance", 0.50)
            if intent == "balance_reset":
                new_bal = 0.50
            elif intent == "balance_tts_up":
                new_bal = min(BAL_MAX, round(cur + VOICE_BAL_STEP, 2))
            else:
                new_bal = max(BAL_MIN, round(cur - VOICE_BAL_STEP, 2))
            ctrl.tts_balance = new_bal
            logger.info(f"[Balance] {intent} → tts_balance={new_bal:.2f}")
            return "tts_balance"

        vol_min = getattr(ctrl, "VOL_MIN", 0.01)
        vol_max = getattr(ctrl, "VOL_MAX", 1.00)

        # radio_mode 優先（即時 fade loop 生效）；否則 stream_mode 改 stream_volume。
        target_attr = "radio_volume" if getattr(ctrl, "radio_mode", False) else "stream_volume"
        current = getattr(ctrl, target_attr, vol_min)

        if intent == "volume_mute":
            new_val = vol_min
        elif intent == "volume_down":
            new_val = max(vol_min, round(current - VOICE_VOL_STEP, 2))
        elif intent == "volume_up":
            new_val = min(vol_max, round(current + VOICE_VOL_STEP, 2))
        else:
            return None

        setattr(ctrl, target_attr, new_val)
        logger.info(f"[Volume] {intent} → {target_attr}={new_val:.2f}")

        # 2026-08-26：TTS 音量跟音樂音量設成同一個值（不是各調各的比例衰減）。
        sync_tts_gain(ctrl, new_val)

        return target_attr

    async def _ack(self, intent: str) -> None:
        text = _ACK_TEXT.get(intent, "好")
        # 文字頻道保底：play_tts 常被 [TTS Silence Gate]（使用者仍在講話）靜默吞掉，
        # 音量調整因此完全沒有回饋、使用者以為指令沒被執行（2026-09-04 實測 case）。
        # pause/stop/skip 都靠 ch.send() 保底，volume 也要比照補上。
        try:
            ch = getattr(self.ctrl, "active_text_channel", None)
            if ch is not None:
                await ch.send(text)
        except Exception:
            logger.exception("[Volume] text ack failed")
        try:
            play_tts = getattr(self.ctrl, "play_tts", None)
            if play_tts is None:
                return
            await play_tts(text, already_in_channel=True)
        except Exception:
            logger.exception("[Volume] ack failed")
