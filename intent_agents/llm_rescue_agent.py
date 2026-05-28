"""LLMRescueAgent — 給 bus no-winner 情境用的 LLM 改寫器（skeleton）。

中文語意常常 regex 抓不到（委婉、反諷、假正向），這個 agent 在所有 regex agent
都沒上 MIN_CONFIDENCE 時介入：

    1. 把使用者原文丟給注入的 async LLM classifier
    2. LLM 回 {rewritten_query, pragmatic_signal, pragmatic_target, confidence}
    3. 信心過門檻 → 用 dataclasses.replace 加料原 ctx，回給 bus 重投
    4. 信心不夠 / LLM 炸 / 改寫空 → 回 None，由 caller 走純對話 fallback

刻意設計：
- 不繼承 DeclarativeIntentAgent：它不參與 bid()，是 bus 的 fallback 階段
- synthesize() 是 async：要呼 LLM
- 例外不傳染：與 bus.bid try/except 同調，LLM 服務炸不能讓整條 wake path 掛
- 不在這個 skeleton 跟 bus 接線：slice 2 才做 wiring + shadow mode
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Awaitable, Callable

from intent_bus import IntentContext

logger = logging.getLogger("cogs.voice_controller.intent_bus.llm_rescue")


LLMClassifier = Callable[[str], Awaitable[dict[str, Any] | None]]


class LLMRescueAgent:
    """LLM-based rescue for un-dispatched intents.

    Caller responsibility (slice 2)：bus 發現 no winner 才呼叫 synthesize()，
    拿到 enriched ctx 後重投 bus.dispatch()。
    """

    name: str = "LLMRescue"

    def __init__(
        self,
        *,
        llm_classifier: LLMClassifier,
        confidence_threshold: float = 0.70,
    ):
        self.llm_classifier = llm_classifier
        self.confidence_threshold = confidence_threshold

    async def synthesize(self, ctx: IntentContext) -> IntentContext | None:
        try:
            result = await self.llm_classifier(ctx.query)
        except Exception as exc:
            logger.warning(f"⚠️ [LLMRescue] classifier 炸了，放棄 rescue: {exc}")
            return None

        if result is None:
            return None

        if result.get("confidence", 0.0) < self.confidence_threshold:
            return None

        rewritten = (result.get("rewritten_query") or "").strip()
        if not rewritten:
            return None

        return replace(
            ctx,
            query=rewritten,
            depth=ctx.depth + 1,
            dispatch_source="llm_rescue",
            pragmatic_signal=result.get("pragmatic_signal"),
            pragmatic_target=result.get("pragmatic_target"),
        )
