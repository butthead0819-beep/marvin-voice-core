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
import re
from dataclasses import replace
from typing import Awaitable, Callable

from intent_bus import Bid, IntentAgent, IntentContext
from intent_judges.regex_judge import regex_judge

_JUDGE_NAME = "cleaner_judge"

CleanerCall = Callable[[IntentContext], Awaitable[str]]

# 與 hallucination_guard_agent 一致的喚醒詞集合（保守 set；此處只用來量「原句剝喚醒
# 後還剩多少實質內容」，判斷 cleaner 是否過度塌縮）。
_WAKE_RE = re.compile(
    "|".join(("馬文", "marvin", "marvy", "麻文", "媽文", "瑪文")), re.IGNORECASE)
# 與 guard rule#3/#4 的「stripped < 3」同一切點：原句剝喚醒後 ≥3 字才算「有實質內容」。
_MIN_NONWAKE_CONTENT = 3


def _nonwake_content_len(text: str) -> int:
    """剝掉喚醒詞與前後標點後剩餘的字數。"""
    return len(_WAKE_RE.sub("", text or "").strip("，,、。.！!？? \t"))


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

    # Cleaner over-collapse guard（cleaner 別毀 query / guard 別誤壓）：弱模型有時把
    # 「馬文，講個笑話給我聽」過度塌縮成只剩「馬文」，regex_judge 讓 HallucinationGuard
    # 出價 swallow。但原句剝喚醒後仍有實質內容 → 這是 cleaner artifact 不是真 STT 幻覺
    # → 不傳播 swallow，回 dense_zero 讓 race 落回 J1 / Marvin fallback（否則整句被靜默）。
    # 原句本身就是純喚醒碎片（真幻覺）時 nonwake_len < 3，不覆寫，guard swallow 照常保留。
    if j1_bid.name == "guard" and _nonwake_content_len(original) >= _MIN_NONWAKE_CONTENT:
        return _dense_zero(f"cleaner_overcollapse:{original}->{cleaned}")

    if j1_bid.confidence == 0.0:
        return _dense_zero(f"cleaned_misses_regex:{original}->{cleaned}")

    return Bid(
        name=j1_bid.name,
        confidence=j1_bid.confidence,
        handler=j1_bid.handler,
        reason=f"j3_cleaned:{original}->{cleaned}|{j1_bid.reason}",
        missing_slots=j1_bid.missing_slots,
    )
