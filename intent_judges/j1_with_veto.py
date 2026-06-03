"""J1 (regex) + J2 (chat veto) 組合 wrapper.

把 chat veto 邏輯封裝成一個 race 可消費的 Bid，race coordinator 完全不用改。

策略：
  1. 跑 regex_judge → j1_bid
  2. 任一短路條件成立 → 直接回 j1_bid（零 LLM 呼叫）：
     - chat_classifier_call is None
     - j1_bid.confidence < fast_path_threshold（J1 本來就不會 fast-path）
     - j1_bid.name not in veto_prone_intents（非歷史 FP 大戶）
  3. 跑 chat_classifier_judge → verdict
  4. verdict.is_chat AND verdict.confidence >= veto_threshold
     → 回降級 Bid（confidence=0.0, handler=noop），race 繼續找 J3
  5. 否則 J2 確認是真意圖 → 回 j1_bid
"""
from __future__ import annotations

from dataclasses import replace
from typing import Awaitable, Callable

from intent_bus import Bid, IntentAgent, IntentContext
from intent_judges.chat_classifier_judge import (
    ChatClassifierCall,
    chat_classifier_judge,
)
from intent_judges.regex_judge import regex_judge


async def _async_noop() -> None:
    pass


async def j1_with_veto(
    ctx: IntentContext,
    agents: list[IntentAgent],
    *,
    chat_classifier_call: ChatClassifierCall | None,
    veto_prone_intents: frozenset[str] = frozenset(),
    fast_path_threshold: float = 0.85,
    veto_threshold: float = 0.80,
    veto_timeout_s: float = 0.5,
) -> Bid:
    j1_bid = regex_judge(ctx, agents)

    # Short-circuit：任一條件成立都直接回 J1，不打 LLM
    if chat_classifier_call is None:
        return j1_bid
    if j1_bid.confidence < fast_path_threshold:
        return j1_bid
    if j1_bid.name not in veto_prone_intents:
        return j1_bid

    verdict = await chat_classifier_judge(
        ctx.raw_text or ctx.query or "",
        j1_bid.name,
        llm_call=chat_classifier_call,
        timeout_s=veto_timeout_s,
    )

    if verdict.is_chat and verdict.confidence >= veto_threshold:
        return Bid(
            name=j1_bid.name,
            confidence=0.0,
            handler=_async_noop,
            reason=(
                f"vetoed_by_chat({verdict.confidence:.2f}):"
                f"{verdict.reason}|orig:{j1_bid.reason}"
            ),
        )

    # J2 執行過但未否決 → 把足跡編進 reason（含 is_chat/conf/verdict.reason）。
    # verdict.reason 涵蓋 llm_timeout / llm_exception / malformed 等 fail-safe 路徑，
    # 讓 shadow outcome 能分辨「J2 健康沒否決」vs「J2 靜默失敗」——否則確認路徑零痕跡、
    # J2 是否真的在跑無法觀測（見 records/judge_outcomes_analysis_2026-06-03.md）。
    return replace(
        j1_bid,
        reason=(
            f"{j1_bid.reason}|j2_ran(chat={verdict.is_chat},"
            f"{verdict.confidence:.2f}):{verdict.reason}"
        ),
    )
