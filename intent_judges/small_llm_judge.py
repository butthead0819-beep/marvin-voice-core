"""J2 SmallLLMJudge — LLM rewriter，靠重用 J1 拿 handler。

流程：
  1. raw STT → llm_call(ctx) → (rewritten_text, llm_confidence)
  2. dataclasses.replace(ctx, query=rewritten, raw_text=rewritten)
  3. regex_judge(rewritten_ctx, agents) → J1 風格 Bid（含 handler）
  4. 最終 confidence = min(j1_confidence, llm_confidence)

llm_call 用 DI 注入，prod 接 Groq 8B / Cerebras，test 傳 fake。
任何 LLM 失敗（exception / timeout / 0 信心 / 空 rewrite）→ dense zero。
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Awaitable, Callable

from intent_bus import Bid, IntentAgent, IntentContext
from intent_judges.regex_judge import regex_judge

_JUDGE_NAME = "small_llm_judge"

LLMCall = Callable[[IntentContext], Awaitable[tuple[str, float]]]


async def _async_noop() -> None:
    pass


def _dense_zero(reason: str) -> Bid:
    return Bid(name=_JUDGE_NAME, confidence=0.0, handler=_async_noop, reason=reason)


async def small_llm_judge(
    ctx: IntentContext,
    agents: list[IntentAgent],
    *,
    llm_call: LLMCall,
    timeout_s: float = 0.5,
) -> Bid:
    original = (ctx.query or "").strip()
    if not original:
        return _dense_zero("empty_query")

    try:
        rewritten, llm_conf = await asyncio.wait_for(llm_call(ctx), timeout=timeout_s)
    except asyncio.TimeoutError:
        return _dense_zero("llm_timeout")
    except Exception:
        return _dense_zero("llm_exception")

    if not rewritten or not rewritten.strip() or llm_conf <= 0.0:
        return _dense_zero("llm_no_rewrite")

    rewritten_ctx = replace(ctx, query=rewritten, raw_text=rewritten)
    j1_bid = regex_judge(rewritten_ctx, agents)
    if j1_bid.confidence == 0.0:
        return _dense_zero(f"rewrite_misses_regex:{original}->{rewritten}")

    final_conf = min(j1_bid.confidence, llm_conf)
    return Bid(
        name=j1_bid.name,
        confidence=final_conf,
        handler=j1_bid.handler,
        reason=f"j2_rewrote:{original}->{rewritten}|{j1_bid.reason}",
        missing_slots=j1_bid.missing_slots,
    )
