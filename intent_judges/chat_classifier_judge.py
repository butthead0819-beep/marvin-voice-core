"""ChatClassifierJudge (J2) — chat-vs-intent 純函數分類器.

2026-05-27 設計：J2 不再是 rewriter，改成 chat veto。

輸入 raw STT + J1 候選 intent name → ChatVerdict（is_chat / confidence / reason）。
LLM 失敗（timeout / exception / malformed / missing fields / wrong types）→
安全 default：is_chat=False, confidence=0.0。這樣不會誤殺 J1 的正向 intent。

caller (J1+veto wrapper) 拿 ChatVerdict 翻譯成 race 可消費的 Bid。Veto 條件
（confidence 門檻、哪些 intent 才檢查）由 caller 決定，本判官只做純分類。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

ChatClassifierCall = Callable[[str, str], Awaitable[dict]]


@dataclass(frozen=True)
class ChatVerdict:
    is_chat: bool
    confidence: float
    reason: str


_SAFE_DEFAULT_REASON_EMPTY = "empty_text"


def _safe_default(reason: str) -> ChatVerdict:
    return ChatVerdict(is_chat=False, confidence=0.0, reason=reason)


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


async def chat_classifier_judge(
    raw_text: str,
    intent_name: str,
    *,
    llm_call: ChatClassifierCall,
    timeout_s: float = 0.5,
) -> ChatVerdict:
    text = (raw_text or "").strip()
    if not text:
        return _safe_default(_SAFE_DEFAULT_REASON_EMPTY)

    try:
        result = await asyncio.wait_for(llm_call(text, intent_name), timeout=timeout_s)
    except asyncio.TimeoutError:
        return _safe_default("llm_timeout")
    except Exception:
        return _safe_default("llm_exception")

    if not isinstance(result, dict):
        return _safe_default("malformed:not_dict")

    if "is_chat" not in result or "confidence" not in result:
        return _safe_default("malformed:missing_fields")

    raw_is_chat = result["is_chat"]
    raw_conf = result["confidence"]
    if not isinstance(raw_is_chat, bool) or not isinstance(raw_conf, (int, float)):
        return _safe_default("malformed:wrong_types")

    reason = str(result.get("reason", "")).strip() or "no_reason"
    return ChatVerdict(
        is_chat=raw_is_chat,
        confidence=_clamp(float(raw_conf)),
        reason=reason,
    )
