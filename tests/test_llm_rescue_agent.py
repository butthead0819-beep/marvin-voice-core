"""LLMRescueAgent skeleton tests.

設計：
當 bus 收不到 above-threshold 的 bid 時，由 LLMRescueAgent 呼叫一個注入的
async LLM classifier（unit test 不打真的 LLM），把使用者原文重寫成一條能被
既有 regex agent 吃下的 query，並在 ctx 帶上 pragmatic_signal/target 讓
handler 知道「字面 vs 真意」的落差。

skeleton 的契約（這個 slice 只測這層）：
- synthesize(ctx) -> IntentContext | None
- LLM 信心 ≥ threshold（預設 0.70）才回 enriched ctx；否則 None
- 回傳 ctx 的 dispatch_source = "llm_rescue"，depth = ctx.depth + 1
- LLM 例外不傳染（與 bus.bid try/except 慣例一致）
- pragmatic_signal / pragmatic_target 從 LLM 結果穿透
- 不在這個 slice 跟 bus 接線（slice 2 才做）
"""
from __future__ import annotations

import pytest

from intent_agents.llm_rescue_agent import LLMRescueAgent
from intent_bus import IntentContext


def _ctx(query="希望下次可以找到好聽的歌", depth=0):
    return IntentContext(
        speaker="alice",
        raw_text=query,
        query=query,
        original_raw=query,
        wake_intent=0.9,
        stream_active=False,
        game_mode=False,
        is_owner=False,
        now=0.0,
        mode="normal",
        depth=depth,
    )


def _make_classifier(payload):
    """Build an async classifier returning a fixed payload (or raising)."""
    async def _classifier(text: str):
        if isinstance(payload, BaseException):
            raise payload
        return payload
    return _classifier


# ── happy path：LLM 高信心 → enriched ctx ─────────────────────────────────────

@pytest.mark.asyncio
async def test_synthesize_returns_enriched_ctx_when_llm_confident():
    """LLM 解析「希望下次播放好聽的歌」→ rewritten=「下一首」+ negative on current_song。
    回傳 ctx 應該帶這些訊號，並標記 dispatch_source=llm_rescue。"""
    classifier = _make_classifier({
        "rewritten_query": "下一首",
        "pragmatic_signal": "negative",
        "pragmatic_target": "current_song",
        "confidence": 0.85,
    })
    agent = LLMRescueAgent(llm_classifier=classifier)

    new_ctx = await agent.synthesize(_ctx())

    assert new_ctx is not None
    assert new_ctx.query == "下一首"
    assert new_ctx.dispatch_source == "llm_rescue"
    assert new_ctx.pragmatic_signal == "negative"
    assert new_ctx.pragmatic_target == "current_song"


@pytest.mark.asyncio
async def test_synthesize_increments_depth_to_block_loop():
    """depth+1 是 bus 既有的迴圈保護機制——LLM rescue 也必須遵守。"""
    classifier = _make_classifier({
        "rewritten_query": "下一首",
        "confidence": 0.9,
    })
    agent = LLMRescueAgent(llm_classifier=classifier)
    new_ctx = await agent.synthesize(_ctx(depth=2))
    assert new_ctx is not None
    assert new_ctx.depth == 3


@pytest.mark.asyncio
async def test_synthesize_preserves_raw_text_for_audit():
    """raw_text 是使用者原始發話，不該被 LLM 改寫覆蓋（feedback log 要追溯字面 vs 真意）。"""
    classifier = _make_classifier({
        "rewritten_query": "下一首",
        "confidence": 0.9,
    })
    agent = LLMRescueAgent(llm_classifier=classifier)
    ctx = _ctx(query="希望下次可以找到好聽的歌")
    new_ctx = await agent.synthesize(ctx)
    assert new_ctx is not None
    assert new_ctx.raw_text == "希望下次可以找到好聽的歌"
    assert new_ctx.query == "下一首"


# ── 信心門檻：低信心 → 不勉強 dispatch ────────────────────────────────────────

@pytest.mark.asyncio
async def test_synthesize_returns_none_when_llm_confidence_below_threshold():
    """LLM rescue 比 regex 不可靠——閾值預設 0.70，低於就放棄（讓 caller 走純對話 fallback）。"""
    classifier = _make_classifier({
        "rewritten_query": "下一首",
        "confidence": 0.5,
    })
    agent = LLMRescueAgent(llm_classifier=classifier)
    assert await agent.synthesize(_ctx()) is None


@pytest.mark.asyncio
async def test_synthesize_threshold_is_configurable():
    """單元測試 / shadow mode 可能要拉低或拉高門檻。"""
    classifier = _make_classifier({
        "rewritten_query": "下一首",
        "confidence": 0.55,
    })
    agent = LLMRescueAgent(llm_classifier=classifier, confidence_threshold=0.5)
    assert await agent.synthesize(_ctx()) is not None


# ── 容錯：LLM 失敗不能炸 bus ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_synthesize_returns_none_on_llm_exception():
    """LLM 服務炸了不能讓整個 dispatch 路徑掛掉——回 None 讓 caller 自處。"""
    classifier = _make_classifier(RuntimeError("groq 503"))
    agent = LLMRescueAgent(llm_classifier=classifier)
    assert await agent.synthesize(_ctx()) is None


@pytest.mark.asyncio
async def test_synthesize_returns_none_when_classifier_returns_none():
    """LLM 主動拒絕分類（如句子太短 / 無意義）→ None。"""
    classifier = _make_classifier(None)
    agent = LLMRescueAgent(llm_classifier=classifier)
    assert await agent.synthesize(_ctx()) is None


@pytest.mark.asyncio
async def test_synthesize_returns_none_when_rewritten_query_empty():
    """LLM 回了但 rewritten 是空字串 → 沒東西可 dispatch，視為失敗。"""
    classifier = _make_classifier({
        "rewritten_query": "  ",
        "confidence": 0.95,
    })
    agent = LLMRescueAgent(llm_classifier=classifier)
    assert await agent.synthesize(_ctx()) is None


# ── 預設值穿透：pragmatic 欄位 LLM 沒給就保持 None ───────────────────────────

@pytest.mark.asyncio
async def test_synthesize_pragmatic_fields_default_to_none_when_llm_omits_them():
    """LLM 認得 intent 但沒偵測到語用落差（surface == pragmatic）→ signal/target 不填，
    handler 看到 None 就跑正常流程，不扣分。"""
    classifier = _make_classifier({
        "rewritten_query": "下一首",
        "confidence": 0.9,
    })
    agent = LLMRescueAgent(llm_classifier=classifier)
    new_ctx = await agent.synthesize(_ctx())
    assert new_ctx is not None
    assert new_ctx.pragmatic_signal is None
    assert new_ctx.pragmatic_target is None
