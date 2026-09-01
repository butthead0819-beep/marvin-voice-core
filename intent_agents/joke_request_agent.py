"""JokeRequestAgent — 使用者喚醒後直接對 Marvin 說「說個笑話」的 intent。

2026-08-31 daily ritual 提報：showay 兩小時內問了兩次「馬文說個笑話來聽聽」，兩次落到
不同 intent_type（social_joke_request / social_talk_request），都沒 agent、被模板 ack
打發（第二次連 ack 都沒有）。決策（Jack 拍板）：複用 DJ 串場的 joke_bank 泛用池隨機
抽一則念出來，零 LLM——中文諧音笑話讓 LLM 現編是能力斷崖（見 memory
project_dj_joke_bank_pinyin_match）。

confidence 0.85，1 個 intent：joke_request。mode_compatible = {"normal", "stream"}
（放歌時也能要笑話，speak() 走 hotswap 注入）。patterns 只含明確「要笑話」的說法，
不含單純出現「笑話」二字（「這個笑話很好笑」不是在要笑話）。
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Awaitable, Callable

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_bus import IntentContext
from joke_bank import get_joke_bank

logger = logging.getLogger(__name__)

# bank 全空（deps 缺 / yaml 壞）時的 fallback——寧可有句厭世回應，不要靜默。
_FALLBACK_LINE = "我腦袋裡的笑話庫現在是空的。……跟我的人生一樣。"


class JokeRequestAgent(DeclarativeIntentAgent):
    name = "joke_request"
    mode_compatible = frozenset({"normal", "stream"})

    def __init__(self, controller):
        self.ctrl = controller
        self._recent: deque[str] = deque(maxlen=8)
        self._intents_cache: list[IntentSchema] | None = None

    def declare_intents(self) -> list[IntentSchema]:
        if self._intents_cache is None:
            self._intents_cache = [
                IntentSchema(
                    "joke_request", 0.85,
                    patterns=[
                        r"(?:說|講|來|聊|表演)(?:個|一個|一則|則|點)?(?:冷)?笑話",
                    ],
                    reason_template="joke_request",
                    manifest_description="使用者要 Marvin 講一個笑話（如「說個笑話」「講笑話來聽」）",
                ),
            ]
        return self._intents_cache

    def make_handler(
        self, schema: IntentSchema, slots: dict, ctx: IntentContext
    ) -> Callable[[], Awaitable[None]]:
        async def _handler() -> None:
            try:
                joke = get_joke_bank().random_joke(exclude=set(self._recent))
            except Exception:
                logger.exception("[JokeRequest] joke_bank 抽取失敗")
                joke = None
            line = joke or _FALLBACK_LINE
            if joke:
                self._recent.append(joke)
            try:
                await self.ctrl.speak(line)
            except Exception:
                logger.exception("[JokeRequest] speak 失敗")

        return _handler
