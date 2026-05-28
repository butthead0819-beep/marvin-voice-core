"""Rescue classifier tests — LLM call → JSON parse → dict for LLMRescueAgent。

設計：
- `make_rescue_classifier(tier_router)` 回一個 async (text) → dict | None 的 closure
- 內部呼叫 `tier_router.quick(json=True)` 拿結構化 JSON 字串
- 容錯：LLM 例外 / 空回應 / malformed JSON / 缺 rewritten_query → None
- 不在這層做信心門檻判斷（LLMRescueAgent 已經有 confidence_threshold）

不打真 LLM；測試只驗 LLM → JSON → dict 這條翻譯鏈，以及容錯。
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from intent_agents.rescue_classifier import make_rescue_classifier


class _FakeRouter:
    """Minimal TieredLLMRouter substitute — 只要 quick(...) 簽名對得上。"""
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    async def quick(self, prompt, *, caller, system=None, max_tokens=200,
                    temperature=0.7, json=False):
        self.calls.append({
            "prompt": prompt, "caller": caller, "system": system,
            "max_tokens": max_tokens, "temperature": temperature, "json": json,
        })
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


# ── happy path ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_classifier_parses_valid_json_into_dict():
    """LLM 回合法 JSON → classifier 回對應 dict。"""
    payload = {
        "rewritten_query": "下一首",
        "pragmatic_signal": "negative",
        "pragmatic_target": "current_song",
        "confidence": 0.85,
    }
    router = _FakeRouter(response=json.dumps(payload, ensure_ascii=False))
    classify = make_rescue_classifier(router)

    result = await classify("希望下次可以找到好聽的歌")

    assert result == payload


@pytest.mark.asyncio
async def test_classifier_requests_json_mode_with_correct_caller_tag():
    """確保 router 收到 json=True + caller="intent_rescue"（per-agent 用量歸屬要）。"""
    router = _FakeRouter(response=json.dumps({"rewritten_query": "x", "confidence": 0.9}))
    classify = make_rescue_classifier(router)
    await classify("hello")

    assert len(router.calls) == 1
    call = router.calls[0]
    assert call["json"] is True
    assert call["caller"] == "intent_rescue"
    # temperature 低 → 給結構化輸出更穩定
    assert call["temperature"] <= 0.3
    # system prompt 必填（teach LLM the schema）
    assert call["system"] is not None
    assert "rewritten_query" in call["system"]


@pytest.mark.asyncio
async def test_classifier_passes_user_text_as_prompt():
    """使用者原文走 user prompt，不走 system（避免 prompt injection 影響規則）。"""
    router = _FakeRouter(response=json.dumps({"rewritten_query": "x", "confidence": 0.9}))
    classify = make_rescue_classifier(router)
    await classify("這首太吵了")

    assert router.calls[0]["prompt"] == "這首太吵了"


@pytest.mark.asyncio
async def test_system_prompt_enforces_taiwan_traditional_chinese():
    """強制台灣繁中：cheap LLM 預設會吐簡體 / 大陸用語，rewritten_query 進 bus
    後撞不到 STT 永遠繁體的 query → rescue 變死規則。Pin 在 system prompt 內。"""
    router = _FakeRouter(response=json.dumps({"rewritten_query": "x", "confidence": 0.9}))
    classify = make_rescue_classifier(router)
    await classify("anything")

    system = router.calls[0]["system"]
    assert "台灣" in system
    assert "繁體" in system
    assert "簡體" in system  # negative example 必須出現給 LLM 對照


# ── 容錯：各種上游失敗都不該炸 ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_classifier_returns_none_when_router_returns_none():
    """provider pool 全 cooldown / TPM 滿 → router.quick 回 None。"""
    classify = make_rescue_classifier(_FakeRouter(response=None))
    assert await classify("x") is None


@pytest.mark.asyncio
async def test_classifier_returns_none_when_router_returns_empty_string():
    """LLM 回空字串（罕見但要 guard）→ None。"""
    classify = make_rescue_classifier(_FakeRouter(response=""))
    assert await classify("x") is None


@pytest.mark.asyncio
async def test_classifier_returns_none_on_malformed_json():
    """LLM 沒遵守 json mode 吐了普通文字 → 不能炸，回 None。"""
    classify = make_rescue_classifier(_FakeRouter(response="不是 JSON 的東西"))
    assert await classify("x") is None


@pytest.mark.asyncio
async def test_classifier_returns_none_when_router_raises():
    """provider 拋例外（network / auth）→ 不傳染到 agent。"""
    classify = make_rescue_classifier(_FakeRouter(response=RuntimeError("groq 503")))
    assert await classify("x") is None


@pytest.mark.asyncio
async def test_classifier_returns_none_when_rewritten_query_missing():
    """LLM 回了 JSON 但 schema 不對（缺必要欄位）→ 視同失敗，不要把噪音餵下游。"""
    classify = make_rescue_classifier(_FakeRouter(
        response=json.dumps({"pragmatic_signal": "negative", "confidence": 0.9})
    ))
    assert await classify("x") is None


@pytest.mark.asyncio
async def test_classifier_returns_none_when_rewritten_query_blank():
    """rewritten_query=""（LLM 拒絕改寫）→ None。LLMRescueAgent 也會擋，但這層先擋掉省 round trip。"""
    classify = make_rescue_classifier(_FakeRouter(
        response=json.dumps({"rewritten_query": "  ", "confidence": 0.9})
    ))
    assert await classify("x") is None


# ── pragmatic 欄位選配性 ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_classifier_works_without_pragmatic_fields():
    """大部分句子無語用落差 — LLM 只回 rewritten_query + confidence 仍要可用。"""
    payload = {"rewritten_query": "下一首", "confidence": 0.95}
    classify = make_rescue_classifier(_FakeRouter(response=json.dumps(payload)))
    result = await classify("再來一個")
    assert result is not None
    assert result.get("pragmatic_signal") is None  # dict.get 預設 None


# ── 整合：可直接餵 LLMRescueAgent(llm_classifier=...) ────────────────────────

@pytest.mark.asyncio
async def test_classifier_output_is_compatible_with_llm_rescue_agent():
    """這層的契約必須讓 LLMRescueAgent 直接吃下去（不額外包裝）。"""
    from intent_agents.llm_rescue_agent import LLMRescueAgent
    from intent_bus import IntentContext

    router = _FakeRouter(response=json.dumps({
        "rewritten_query": "下一首",
        "pragmatic_signal": "negative",
        "pragmatic_target": "current_song",
        "confidence": 0.85,
    }))
    classify = make_rescue_classifier(router)
    agent = LLMRescueAgent(llm_classifier=classify)

    ctx = IntentContext(
        speaker="Alice", raw_text="希望下次可以找到好聽的歌",
        query="希望下次可以找到好聽的歌", original_raw="希望下次可以找到好聽的歌",
        wake_intent=0.9, stream_active=False, game_mode=False,
        is_owner=False, now=0.0,
    )
    rescued = await agent.synthesize(ctx)
    assert rescued is not None
    assert rescued.query == "下一首"
    assert rescued.pragmatic_signal == "negative"
    assert rescued.pragmatic_target == "current_song"
    assert rescued.dispatch_source == "llm_rescue"
