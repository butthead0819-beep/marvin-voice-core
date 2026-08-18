"""TimeQueryAgent — 「現在幾點」報時 intent.

對應 records/agent_gaps.jsonl 分析（2026-08-18）：time_query 5 筆/2 distinct，
已達 READY_THRESHOLD=2，但這個查詢完全不需要 LLM——讀系統時鐘即可，比走
gap classifier 的 cheap-LLM cascade 還便宜（零成本、零延遲）。

confidence 0.90，1 個 intent：time_query。
mode_compatible = {"normal", "stream"}（遊戲模式不該誤觸發）。
無 gate：報時跟播放狀態無關，隨時可答。
"""
from __future__ import annotations

import datetime
import logging
from typing import Awaitable, Callable

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_bus import IntentContext

logger = logging.getLogger(__name__)

# Asia/Taipei timezone — bot deploys in 台灣，跟 intent_agents/recommendation.py 同慣例。
_TPE_TZ = datetime.timezone(datetime.timedelta(hours=8))


class TimeQueryAgent(DeclarativeIntentAgent):
    name = "time_query"
    mode_compatible = frozenset({"normal", "stream"})

    def __init__(self, controller):
        self.ctrl = controller
        self._intents_cache: list[IntentSchema] | None = None

    def declare_intents(self) -> list[IntentSchema]:
        if self._intents_cache is None:
            self._intents_cache = [
                IntentSchema(
                    "time_query", 0.90,
                    patterns=[r"現在幾點|幾點了|現在(是)?什麼時間|報時"],
                    reason_template="time_query:{matched}",
                ),
            ]
        return self._intents_cache

    def make_handler(
        self, schema: IntentSchema, slots: dict, ctx: IntentContext
    ) -> Callable[[], Awaitable[None]]:
        async def _handler() -> None:
            now = datetime.datetime.now(_TPE_TZ)
            text = f"現在是{now.hour}點{now.minute}分"
            try:
                play_tts = getattr(self.ctrl, "play_tts", None)
                if play_tts is None:
                    logger.warning("[TimeQuery] ctrl.play_tts 不存在，skip")
                    return
                await play_tts(text, already_in_channel=True)
            except Exception:
                logger.exception("[TimeQuery] 報時失敗")

        return _handler
