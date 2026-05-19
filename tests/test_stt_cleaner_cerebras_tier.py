"""TDD：stt_cleaner.py Tier 1.5 — Groq 8b 429 → Cerebras 8b → Groq 70b.

Why: Dev Tier 被擋（2026-05-19），Cerebras llama-3.1-8b 延遲 ~100ms 比 Groq
還快，TPM 限制寬鬆，是 Groq 8b 失效時的主要 TPM 救援路徑。

驗收：Groq 8b 命中 429 時，Cerebras 路徑要先被呼叫（在 Groq 70b 之前）。
Cerebras 成功 → 不打 70b；Cerebras 失敗 → 才打 70b。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from stt_cleaner import GeminiRouterSTTMixin


def _fake_groq_response(cleaned: str = "馬文，播放音樂", intent: float = 1.0,
                       calling: bool = True, is_complete: bool = True,
                       total_tokens: int = 50):
    """模擬 OpenAI-compat chat.completions response。"""
    content = json.dumps({
        "cleaned": cleaned,
        "intent": intent,
        "calling": calling,
        "is_complete": is_complete,
    }, ensure_ascii=False)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(total_tokens=total_tokens),
    )


def _make_router(groq_8b_raises_429: bool = False,
                 cerebras_succeeds: bool = True,
                 cerebras_client_exists: bool = True,
                 cerebras_raises: bool = False):
    """建一個帶 Mixin 的假 router，覆蓋必要欄位。"""
    class _R(GeminiRouterSTTMixin):
        pass

    r = _R()
    r.groq_cleaner_usage = []
    r.wake_fusion = None
    r.prompt_manager = MagicMock()
    r.prompt_manager.get_instruction = MagicMock(return_value="SYS_PROMPT")
    r.google_cleaner_client = None  # 不走 Gemini 路徑

    # Groq client
    groq = MagicMock()
    if groq_8b_raises_429:
        groq.chat.completions.create = AsyncMock(
            side_effect=Exception("rate_limit_exceeded: try again in 5.0s")
        )
    else:
        groq.chat.completions.create = AsyncMock(return_value=_fake_groq_response())
    r.groq_dedicated_client = groq
    r.groq_fallback_model = "llama-3.3-70b-versatile"

    # Cerebras client
    if cerebras_client_exists:
        cerebras = MagicMock()
        if cerebras_raises:
            cerebras.chat.completions.create = AsyncMock(
                side_effect=Exception("cerebras transient error")
            )
        elif cerebras_succeeds:
            cerebras.chat.completions.create = AsyncMock(
                return_value=_fake_groq_response(cleaned="馬文，播放周杰倫")
            )
        r.cerebras_client = cerebras
        r.cerebras_model = "llama-3.1-8b"
    else:
        r.cerebras_client = None
        r.cerebras_model = None

    return r


@pytest.mark.asyncio
async def test_groq_8b_success_skips_cerebras():
    """Groq 8b 正常時，Cerebras 不該被呼叫（不破壞 happy path）。"""
    r = _make_router(groq_8b_raises_429=False)
    res = await r.clean_stt_text("馬文播放音樂", speaker="大肚")
    assert res["text"] == "馬文，播放音樂"
    assert r.groq_dedicated_client.chat.completions.create.await_count == 1
    assert r.cerebras_client.chat.completions.create.await_count == 0


@pytest.mark.asyncio
async def test_groq_8b_429_falls_through_to_cerebras():
    """Groq 8b 429 → Cerebras 被呼叫且結果被使用，不該打 Groq 70b。"""
    r = _make_router(groq_8b_raises_429=True, cerebras_succeeds=True)
    res = await r.clean_stt_text("馬文播放周杰倫", speaker="大肚")
    assert res["text"] == "馬文，播放周杰倫", "應該回傳 Cerebras 結果"

    # Groq 8b 被打了 1 次（429）
    assert r.groq_dedicated_client.chat.completions.create.await_count == 1
    # Cerebras 被打了 1 次
    assert r.cerebras_client.chat.completions.create.await_count == 1


@pytest.mark.asyncio
async def test_cerebras_failure_falls_through_to_groq_70b():
    """Cerebras 失敗 → 才繼續打 Groq 70b（保留現有 fallback chain）。"""
    r = _make_router(groq_8b_raises_429=True, cerebras_raises=True)

    # 第二次 groq call（70b）需要成功
    r.groq_dedicated_client.chat.completions.create = AsyncMock(
        side_effect=[
            Exception("rate_limit_exceeded: try again in 5.0s"),  # 8b
            _fake_groq_response(cleaned="馬文，播放音樂 70b"),     # 70b
        ]
    )

    res = await r.clean_stt_text("馬文播放音樂", speaker="大肚")
    assert res["text"] == "馬文，播放音樂 70b"
    assert r.cerebras_client.chat.completions.create.await_count == 1
    assert r.groq_dedicated_client.chat.completions.create.await_count == 2


@pytest.mark.asyncio
async def test_cerebras_client_missing_falls_through_to_groq_70b():
    """Cerebras client 沒 configure 時，直接跳 Groq 70b 不該炸。"""
    r = _make_router(groq_8b_raises_429=True, cerebras_client_exists=False)

    r.groq_dedicated_client.chat.completions.create = AsyncMock(
        side_effect=[
            Exception("rate_limit_exceeded: try again in 5.0s"),
            _fake_groq_response(cleaned="馬文，播放音樂 70b"),
        ]
    )

    res = await r.clean_stt_text("馬文播放音樂", speaker="大肚")
    assert res["text"] == "馬文，播放音樂 70b"
    assert r.groq_dedicated_client.chat.completions.create.await_count == 2


@pytest.mark.asyncio
async def test_cerebras_uses_json_response_format():
    """Cerebras call 必須帶 response_format=json_object，回傳格式才能被 _validate_cleaned 接。"""
    r = _make_router(groq_8b_raises_429=True, cerebras_succeeds=True)
    await r.clean_stt_text("馬文播放音樂", speaker="大肚")

    call_kwargs = r.cerebras_client.chat.completions.create.call_args.kwargs
    assert call_kwargs.get("response_format") == {"type": "json_object"}, \
        "Cerebras 必須帶 JSON response_format，否則回傳純文字無法被 _validate_cleaned 解析"
    assert call_kwargs.get("temperature") == 0.0, \
        "Cleaner 任務需 deterministic，temperature 必須是 0"


@pytest.mark.asyncio
async def test_cerebras_cooldown_after_429():
    """Cerebras 429 → 進冷卻；冷卻期內第二次呼叫直接跳過 Cerebras 打 Groq 70b。"""
    r = _make_router(groq_8b_raises_429=True)
    # 第一次：Groq 8b 429 + Cerebras 429
    r.cerebras_client.chat.completions.create = AsyncMock(
        side_effect=Exception("rate_limit_exceeded: try again in 30.0s")
    )
    r.groq_dedicated_client.chat.completions.create = AsyncMock(
        side_effect=[
            Exception("rate_limit_exceeded: try again in 5.0s"),  # 8b
            _fake_groq_response(cleaned="馬文，第一次 70b"),       # 70b
            Exception("rate_limit_exceeded: try again in 5.0s"),  # 8b 第二次
            _fake_groq_response(cleaned="馬文，第二次 70b"),       # 70b 第二次
        ]
    )

    await r.clean_stt_text("馬文播放音樂", speaker="大肚")
    # 第二次：Cerebras 冷卻中應跳過
    await r.clean_stt_text("馬文播放音樂", speaker="大肚")

    # Cerebras 只被打 1 次（第二次跳過）
    assert r.cerebras_client.chat.completions.create.await_count == 1
