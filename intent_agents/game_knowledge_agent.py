"""GameKnowledgeAgent — 遊戲知識查詢（declarative intent）。

來源：2026-06-06 intent_gap 累積 3 個 distinct「馬文幫我查麥塊…」樣本，
`game_knowledge_query` 標 ready_to_implement=true（見 records/agent_gaps.jsonl）。
在此之前這類問句只拿到 intent_gap 的模板 ack，沒有真正回答。

設計（最小）：
  - trigger = 查/查詢 + 遊戲 marker（封閉集合，非無止境調 pattern）；
    刻意要求 marker，避免吃掉「查歌詞 / 查資料」這類非遊戲查詢。
  - handler 把整句交給 controller._handle_game_knowledge_query → 走 Marvin LLM 回答 + TTS。
  - 知識來源走既有 LLM bus（Marvin 已知主流遊戲常識）；未來要更準可在 handler 內加 web search。
"""
from __future__ import annotations

import re

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_bus import IntentContext


# 遊戲 marker 封閉集合 — 隨 intent_gap 出現新遊戲再擴，不無止境調 regex。
GAME_KNOWLEDGE_MARKERS = (
    "麥塊", "當個創世神", "我的世界", "minecraft",
    "傳說對決", "英雄聯盟", "lol", "原神", "寶可夢", "瑪利歐",
    "艾爾登", "elden", "遊戲",
)


def _marker_alt() -> str:
    return "|".join(re.escape(m) for m in sorted(GAME_KNOWLEDGE_MARKERS, key=len, reverse=True))


class GameKnowledgeAgent(DeclarativeIntentAgent):
    name = "game_knowledge"
    # 遊戲知識查詢是一般資訊請求，normal/stream 都活著；game session 模式（busted 等）不出價。
    mode_compatible = frozenset({"normal", "stream"})

    def __init__(self, controller):
        self.ctrl = controller
        self._cache: list[IntentSchema] | None = None

    def declare_intents(self) -> list[IntentSchema]:
        if self._cache is None:
            marker = _marker_alt()
            self._cache = [
                IntentSchema(
                    "game_knowledge_query", 0.80,
                    patterns=[rf"(?:幫我|幫忙|麻煩|想|請)?\s*查(?:詢|一下)?.{{0,12}}?(?P<game>{marker})"],
                    reason_template="game_knowledge:{game}",
                ),
            ]
        return self._cache

    def make_handler(self, schema, slots, ctx: IntentContext):
        async def _answer():
            await self.ctrl._handle_game_knowledge_query(ctx.speaker, ctx.query)
        return _answer
