"""FarewellAgent — 使用者喚醒後直接對 Marvin 說「掰掰/晚安/bye bye」的互道再見 intent。

8/9 使用者提報：喚醒說「Marvin byebye」或「馬文晚安」/「馬文再見」，Marvin 沒有真的
道別、也沒有 TTS。根因是 `_query_quality_gate` 把這幾個短道別詞當成雜訊擋在 bus
之前（too_short/ambient_statement），query 根本沒機會走到這個 agent。已在
voice_controller.py 補 `_DIRECT_FAREWELL_WORDS` 短路放行；這個 agent 接手後续。

patterns 是「純道別詞」子集（不含 _FAREWELL_RE 裡「先走了/我要下線」這類離場宣告——
那些是側通道 `_handle_farewell_speech` 的範疇，預測會不會離場，不是使用者在跟 Marvin
打招呼）。DIRECT_FAREWELL_WORDS 也是 voice_controller._query_quality_gate 的短路
放行清單——single source of truth，兩邊都 import 這裡，別各自維護一份。

confidence 0.90，1 個 intent：farewell。mode_compatible = {"normal", "stream"}：
音樂播放中也該能互道再見，`generate_player_farewell(stream_active=...)` 已處理
≤30 字的 hotswap 插話限制。
"""
from __future__ import annotations

import logging
import re
from typing import Awaitable, Callable

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_bus import IntentContext

logger = logging.getLogger(__name__)

# 純道別詞（喚醒後整句就是這個）。voice_controller._query_quality_gate 用它放行
# 短句過 too_short/ambient_statement 閘；本 agent 的 pattern 也由它組出，別重複維護。
DIRECT_FAREWELL_WORDS: frozenset[str] = frozenset({
    "晚安", "再見", "掰掰", "拜拜", "掰了", "掰", "拜了個拜",
    "byebye", "bye bye", "goodbye", "good bye", "goodnight", "good night",
})


class FarewellAgent(DeclarativeIntentAgent):
    name = "farewell"
    mode_compatible = frozenset({"normal", "stream"})

    def __init__(self, controller):
        self.ctrl = controller
        self._intents_cache: list[IntentSchema] | None = None

    def declare_intents(self) -> list[IntentSchema]:
        if self._intents_cache is None:
            alt = "|".join(re.escape(w) for w in sorted(DIRECT_FAREWELL_WORDS, key=len, reverse=True))
            self._intents_cache = [
                IntentSchema(
                    "farewell", 0.90,
                    patterns=[
                        rf'(?:^|[\s，,、！!？?.。]+)(?:{alt})(?:[\s，,、！!？?.。]|$)',
                    ],
                    reason_template="farewell:{matched}",
                ),
            ]
        return self._intents_cache

    def make_handler(
        self, schema: IntentSchema, slots: dict, ctx: IntentContext
    ) -> Callable[[], Awaitable[None]]:
        speaker = ctx.speaker

        async def _handler() -> None:
            handle = getattr(self.ctrl, "_handle_wake_farewell", None)
            if handle is None:
                logger.warning("[Farewell] ctrl 沒裝 _handle_wake_farewell，跳過")
                return
            try:
                await handle(speaker)
            except Exception:
                logger.exception("[Farewell] _handle_wake_farewell failed")

        return _handler
