"""J3 ClenerJudge — race 的 slow fallback，包裝 stt_cleaner.py。

流程：
  1. raw STT → cleaner_call(ctx) → cleaned_text（過幻覺過濾 / 碎片清理）
  2. dataclasses.replace(ctx, query=cleaned, raw_text=cleaned)
  3. regex_judge(cleaned_ctx, agents) → J1 風格 Bid（含 handler）
  4. 直接回 J1 信心（cleaner 不自報信心 → 沒 cap）

cleaner_call DI 注入，prod 接 stt_cleaner.py 的 clean coroutine。
任何 cleaner 失敗（exception / timeout / 回空）→ dense zero，race 退到 best-conf fallback。

cleaner 回空時刻意視為「cleaner dropped」，不再下游硬跑 regex —— 對齊現行 hallucination
filter 行為（STT 引擎注入 context strings 同時也是幻覺來源，cleaner 過濾後若空就跳過）。
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Awaitable, Callable

from intent_bus import Bid, IntentAgent, IntentContext
from intent_judges.regex_judge import regex_judge

_JUDGE_NAME = "cleaner_judge"

CleanerCall = Callable[[IntentContext], Awaitable[str]]


async def _async_noop() -> None:
    pass


def _dense_zero(reason: str) -> Bid:
    return Bid(name=_JUDGE_NAME, confidence=0.0, handler=_async_noop, reason=reason)


async def cleaner_judge(
    ctx: IntentContext,
    agents: list[IntentAgent],
    *,
    cleaner_call: CleanerCall,
    timeout_s: float = 1.5,
) -> Bid:
    original = (ctx.query or "").strip()
    if not original:
        return _dense_zero("empty_query")

    try:
        cleaned = await asyncio.wait_for(cleaner_call(ctx), timeout=timeout_s)
    except asyncio.TimeoutError:
        return _dense_zero("cleaner_timeout")
    except Exception:
        return _dense_zero("cleaner_exception")

    cleaned = (cleaned or "").strip()
    if not cleaned:
        return _dense_zero("cleaner_dropped_empty")

    cleaned_ctx = replace(ctx, query=cleaned, raw_text=cleaned)
    j1_bid = regex_judge(cleaned_ctx, agents)
    if j1_bid.confidence == 0.0:
        return _dense_zero(f"cleaned_misses_regex:{original}->{cleaned}")

    return Bid(
        name=j1_bid.name,
        confidence=j1_bid.confidence,
        handler=j1_bid.handler,
        reason=f"j3_cleaned:{original}->{cleaned}|{j1_bid.reason}",
        missing_slots=j1_bid.missing_slots,
    )
