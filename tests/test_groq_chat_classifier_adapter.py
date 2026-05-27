"""Groq chat classifier adapter — 把 TieredLLMRouter 包成 ChatClassifierCall.

純 adapter 層：caller 傳 TieredLLMRouter，回 ChatClassifierCall：
  (raw_text, intent_name) → dict {"is_chat": bool, "confidence": float, "reason": str}

router.quick() 回 None（pool 全冷卻）→ 安全 default：is_chat=False, confidence=0.0
router.quick() 回非 JSON 字串 → 讓 chat_classifier_judge 的 exception path 接住
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


def _router_returning(text: str | None) -> MagicMock:
    """Mock TieredLLMRouter，.quick() 回指定字串或 None。"""
    router = MagicMock()
    router.quick = AsyncMock(return_value=text)
    return router


# ── happy path ────────────────────────────────────────────────────────────


async def test_returns_dict_when_router_returns_valid_json():
    from intent_judges.groq_chat_classifier_adapter import (
        make_groq_chat_classifier,
    )

    router = _router_returning(json.dumps({
        "is_chat": True, "confidence": 0.90, "reason": "modal:應該"
    }))
    call = make_groq_chat_classifier(router)
    result = await call("應該下一首就是", "skip")
    assert result == {"is_chat": True, "confidence": 0.90, "reason": "modal:應該"}


async def test_passes_caller_attribution():
    """usage 必須以 caller='chat_classifier' 歸屬，方便 per-agent token tracking。"""
    from intent_judges.groq_chat_classifier_adapter import (
        make_groq_chat_classifier,
    )

    router = _router_returning('{"is_chat": false, "confidence": 0.95, "reason": "x"}')
    call = make_groq_chat_classifier(router)
    await call("下一首", "skip")
    _, kwargs = router.quick.call_args
    assert kwargs.get("caller") == "chat_classifier"


async def test_passes_json_mode():
    """response_format = json_object 必須啟用，避免 LLM 回 plain text。"""
    from intent_judges.groq_chat_classifier_adapter import (
        make_groq_chat_classifier,
    )

    router = _router_returning('{"is_chat": false, "confidence": 0.9, "reason": "x"}')
    call = make_groq_chat_classifier(router)
    await call("x", "skip")
    _, kwargs = router.quick.call_args
    assert kwargs.get("json") is True


async def test_includes_raw_and_intent_in_user_prompt():
    """user prompt 必須含 raw + intent name，否則 LLM 看不到判斷依據。"""
    from intent_judges.groq_chat_classifier_adapter import (
        make_groq_chat_classifier,
    )

    router = _router_returning('{"is_chat": false, "confidence": 0.9, "reason": "x"}')
    call = make_groq_chat_classifier(router)
    await call("應該下一首", "skip")
    args, kwargs = router.quick.call_args
    # prompt 可能是 positional 或 keyword
    prompt = kwargs.get("prompt") or (args[0] if args else "")
    assert "應該下一首" in prompt
    assert "skip" in prompt


async def test_low_temperature_for_consistency():
    """分類任務 temperature 要低，避免重複問同一句答案飄。"""
    from intent_judges.groq_chat_classifier_adapter import (
        make_groq_chat_classifier,
    )

    router = _router_returning('{"is_chat": false, "confidence": 0.9, "reason": "x"}')
    call = make_groq_chat_classifier(router)
    await call("x", "skip")
    _, kwargs = router.quick.call_args
    assert kwargs.get("temperature", 1.0) <= 0.2


# ── safe fallback ─────────────────────────────────────────────────────────


async def test_pool_exhausted_returns_safe_default():
    """router 全冷卻回 None → 安全 default（不誤殺正向 intent）。"""
    from intent_judges.groq_chat_classifier_adapter import (
        make_groq_chat_classifier,
    )

    router = _router_returning(None)
    call = make_groq_chat_classifier(router)
    result = await call("應該下一首", "skip")
    assert result.get("is_chat") is False
    assert result.get("confidence") == 0.0


async def test_malformed_json_propagates_exception():
    """非合法 JSON → raise；chat_classifier_judge 的 except 路徑接住，
    回 safe default。本 adapter 不吞 exception。"""
    from intent_judges.groq_chat_classifier_adapter import (
        make_groq_chat_classifier,
    )

    router = _router_returning("this is not json")
    call = make_groq_chat_classifier(router)
    with pytest.raises(Exception):
        await call("x", "skip")


# ── 整合：chat_classifier_judge + groq adapter ────────────────────────────


async def test_integration_with_chat_classifier_judge():
    """E2E：把 adapter 餵給 chat_classifier_judge，整條鏈跑得通。"""
    from intent_judges.chat_classifier_judge import chat_classifier_judge
    from intent_judges.groq_chat_classifier_adapter import (
        make_groq_chat_classifier,
    )

    router = _router_returning('{"is_chat": true, "confidence": 0.88, "reason": "non_music_target:網站"}')
    call = make_groq_chat_classifier(router)
    verdict = await chat_classifier_judge(
        "麻煩幫我找到這個詭異的線上網站", "music", llm_call=call,
    )
    assert verdict.is_chat is True
    assert verdict.confidence == 0.88
    assert "網站" in verdict.reason


async def test_integration_pool_exhausted_safe_verdict():
    """E2E：pool 滿時 chat_classifier_judge 收到的 dict 不會 veto。"""
    from intent_judges.chat_classifier_judge import chat_classifier_judge
    from intent_judges.groq_chat_classifier_adapter import (
        make_groq_chat_classifier,
    )

    router = _router_returning(None)
    call = make_groq_chat_classifier(router)
    verdict = await chat_classifier_judge("應該下一首", "skip", llm_call=call)
    assert verdict.is_chat is False
    assert verdict.confidence == 0.0
