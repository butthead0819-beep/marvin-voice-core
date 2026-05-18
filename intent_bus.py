"""
IntentBus — wake 之後的意圖路由 (Phase 1)。

把原本 _process_queued_query 的 if/elif fast-track chain 轉成顯式廣播：
  1. 每個 IntentAgent 看 IntentContext，回 Bid(confidence, handler) 或 None
  2. Bus collect 所有 bids → 取最高 confidence
  3. 若最高 < MIN_CONFIDENCE → 沒人接，caller 自處
  4. 否則 await winner.handler() 執行

設計刻意：
- bid() sync + fast (≤5ms)：禁止 LLM 呼叫 / I/O；昂貴判斷放 handler 內
- IntentContext frozen：agent 不能誤改 state
- agent 例外不傳染：一個 agent 炸，其他繼續 bid
- handler 例外往上拋：由 caller 決定要不要兜底
- 同分穩定排序：取第一個註冊的，方便 debug
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

# 用 cogs.voice_controller 的子 logger，繼承 main_discord.py 設的 INFO level；
# 否則 root logger 是 WARNING，INFO 級的 dispatch log 會全部被吞掉。
logger = logging.getLogger("cogs.voice_controller.intent_bus")


@dataclass(frozen=True)
class IntentContext:
    """Wake event 的全部 context，傳給每個 agent 看。

    Frozen 是防呆 — agent 不能在 bid() 內 mutate state。
    """
    speaker: str
    raw_text: str
    query: str
    original_raw: str | None
    wake_intent: float | None
    stream_active: bool
    game_mode: bool
    is_owner: bool
    now: float


@dataclass
class Bid:
    name: str
    confidence: float           # 0.0–1.0
    handler: Callable[[], Awaitable[None]]
    reason: str = ""


class IntentAgent(Protocol):
    name: str
    def bid(self, ctx: IntentContext) -> Bid | None: ...


class IntentBus:
    MIN_CONFIDENCE = 0.30

    def __init__(self, agents: list[IntentAgent]):
        self.agents = list(agents)
        self.logger = logger

    async def dispatch(self, ctx: IntentContext) -> Bid | None:
        """收 bids、選 winner、await handler。回傳 winner Bid（or None 如果沒人 above threshold）。"""
        bids: list[Bid] = []
        for agent in self.agents:
            try:
                b = agent.bid(ctx)
            except Exception as exc:
                self.logger.warning(
                    f"⚠️ [IntentBus] {getattr(agent, 'name', '?')} bid() 炸了，跳過: {exc}"
                )
                continue
            if b is not None:
                bids.append(b)

        bid_summary = ", ".join(f"{b.name}={b.confidence:.2f}({b.reason})" for b in bids) or "no_bids"

        if not bids:
            self.logger.info(
                f"📡 [IntentBus] speaker={ctx.speaker} query='{ctx.query[:50]}' "
                f"wake_intent={ctx.wake_intent} bids: {bid_summary} winner=none"
            )
            return None

        # 同分取第一個（list.sort 是 stable）— 從 max() 改用 sort 確保穩定
        bids.sort(key=lambda b: b.confidence, reverse=True)
        winner = bids[0]

        if winner.confidence < self.MIN_CONFIDENCE:
            self.logger.info(
                f"📡 [IntentBus] speaker={ctx.speaker} query='{ctx.query[:50]}' "
                f"wake_intent={ctx.wake_intent} bids: {bid_summary} "
                f"winner=none (max={winner.confidence:.2f}<{self.MIN_CONFIDENCE})"
            )
            return None

        self.logger.info(
            f"📡 [IntentBus] speaker={ctx.speaker} query='{ctx.query[:50]}' "
            f"wake_intent={ctx.wake_intent} bids: {bid_summary} winner={winner.name}"
        )
        await winner.handler()
        return winner
