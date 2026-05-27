"""ChatClassifierJudge (J2) — chat-vs-intent 純函數分類器.

2026-05-27 設計轉向（議題 E 後重新討論）：
  J2 不再是 rewriter（J3 cleaner 已覆蓋），改成 **chat veto** 角色。
  輸入 raw STT + J1 候選 intent name → 判斷是真意圖還是純對話。

回 ChatVerdict（非 Bid）—— 因為 J2 不爭 race winner，verdict 由 J1+veto wrapper
翻譯成 race 可消費的 Bid。

LLM 失敗（timeout/exception/malformed JSON/missing fields）→ 安全 default：
  is_chat=False, confidence=0.0
這樣不會誤殺 J1 的正向 intent。
"""
from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.asyncio


# ── ChatVerdict 結構 ───────────────────────────────────────────────────────


async def test_chat_verdict_has_three_fields():
    from intent_judges.chat_classifier_judge import ChatVerdict
    v = ChatVerdict(is_chat=True, confidence=0.85, reason="modal:應該")
    assert v.is_chat is True
    assert v.confidence == 0.85
    assert v.reason == "modal:應該"


async def test_chat_verdict_is_frozen():
    """immutable，避免下游意外改值。"""
    from intent_judges.chat_classifier_judge import ChatVerdict
    v = ChatVerdict(is_chat=True, confidence=0.85, reason="x")
    with pytest.raises((AttributeError, Exception)):
        v.is_chat = False  # type: ignore


# ── happy path ─────────────────────────────────────────────────────────────


async def test_returns_chat_verdict_when_llm_says_chat():
    from intent_judges.chat_classifier_judge import chat_classifier_judge

    async def _llm(raw, intent_name):
        assert raw == "應該下一首就是"
        assert intent_name == "skip"
        return {"is_chat": True, "confidence": 0.90, "reason": "modal:應該"}

    v = await chat_classifier_judge("應該下一首就是", "skip", llm_call=_llm)
    assert v.is_chat is True
    assert v.confidence == 0.90
    assert v.reason == "modal:應該"


async def test_returns_intent_verdict_when_llm_says_intent():
    from intent_judges.chat_classifier_judge import chat_classifier_judge

    async def _llm(raw, intent_name):
        return {"is_chat": False, "confidence": 0.95, "reason": "strong_keyword:下一首"}

    v = await chat_classifier_judge("下一首", "skip", llm_call=_llm)
    assert v.is_chat is False
    assert v.confidence == 0.95


# ── safe defaults on failure ──────────────────────────────────────────────


async def test_empty_text_returns_safe_default():
    from intent_judges.chat_classifier_judge import chat_classifier_judge
    called = [False]

    async def _llm(raw, intent_name):
        called[0] = True
        return {"is_chat": True, "confidence": 1.0, "reason": "x"}

    v = await chat_classifier_judge("", "skip", llm_call=_llm)
    assert v.is_chat is False  # 不誤殺
    assert v.confidence == 0.0
    assert called[0] is False  # 空字串不該打 LLM


async def test_llm_exception_returns_safe_default():
    from intent_judges.chat_classifier_judge import chat_classifier_judge

    async def _llm(raw, intent_name):
        raise RuntimeError("groq down")

    v = await chat_classifier_judge("應該下一首", "skip", llm_call=_llm)
    assert v.is_chat is False
    assert v.confidence == 0.0
    assert "exception" in v.reason


async def test_llm_timeout_returns_safe_default():
    from intent_judges.chat_classifier_judge import chat_classifier_judge

    async def _slow_llm(raw, intent_name):
        await asyncio.sleep(2.0)
        return {"is_chat": True, "confidence": 1.0, "reason": "x"}

    v = await chat_classifier_judge(
        "應該下一首", "skip", llm_call=_slow_llm, timeout_s=0.05,
    )
    assert v.is_chat is False
    assert "timeout" in v.reason


async def test_malformed_dict_missing_is_chat_returns_safe_default():
    from intent_judges.chat_classifier_judge import chat_classifier_judge

    async def _llm(raw, intent_name):
        return {"confidence": 0.9, "reason": "x"}  # 缺 is_chat

    v = await chat_classifier_judge("x", "skip", llm_call=_llm)
    assert v.is_chat is False
    assert "malformed" in v.reason or "missing" in v.reason


async def test_malformed_dict_missing_confidence_returns_safe_default():
    from intent_judges.chat_classifier_judge import chat_classifier_judge

    async def _llm(raw, intent_name):
        return {"is_chat": True, "reason": "x"}  # 缺 confidence

    v = await chat_classifier_judge("x", "skip", llm_call=_llm)
    assert v.is_chat is False


async def test_non_dict_response_returns_safe_default():
    from intent_judges.chat_classifier_judge import chat_classifier_judge

    async def _llm(raw, intent_name):
        return "not a dict"

    v = await chat_classifier_judge("x", "skip", llm_call=_llm)
    assert v.is_chat is False


async def test_wrong_types_in_response_returns_safe_default():
    from intent_judges.chat_classifier_judge import chat_classifier_judge

    async def _llm(raw, intent_name):
        return {"is_chat": "yes", "confidence": "high", "reason": "x"}  # 型別錯

    v = await chat_classifier_judge("x", "skip", llm_call=_llm)
    assert v.is_chat is False


# ── confidence 邊界 ───────────────────────────────────────────────────────


async def test_confidence_clamped_to_unit_interval():
    """LLM 回 1.5 / -0.2 等異常值 → clamp 到 [0.0, 1.0]。"""
    from intent_judges.chat_classifier_judge import chat_classifier_judge

    async def _high(raw, intent_name):
        return {"is_chat": True, "confidence": 1.5, "reason": "x"}

    async def _neg(raw, intent_name):
        return {"is_chat": True, "confidence": -0.2, "reason": "x"}

    v_high = await chat_classifier_judge("x", "skip", llm_call=_high)
    v_neg = await chat_classifier_judge("x", "skip", llm_call=_neg)
    assert 0.0 <= v_high.confidence <= 1.0
    assert 0.0 <= v_neg.confidence <= 1.0
