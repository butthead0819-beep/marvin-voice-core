"""TDD — Groq 推理模型（如 gpt-oss-120b）常把 token 燒在內部推理，回傳空字串內容。

空字串跟 None 應該同樣觸發 fallback 到 Gemini/cloud，而不是被 `summary is None`
的判斷放過、一路混到 `summary.strip().splitlines()[0]` 才用空 list 索引炸掉
（觀測到的真實症狀：bot_main.log 每輪都印 `Slow summary failed: list index
out of range`，跟真正的 LLM 判斷 SKIP／雲端全滅 完全是兩回事，卻被外層 except
吞掉偽裝成「內容不值得記錄」）。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gemini_router_content import GeminiRouterContentMixin


def _make_fake_router(*, groq_content: str | None):
    fake = MagicMock()
    fake.last_slow_summary = None
    fake.dna = None
    fake.memory = None
    fake.prompt_manager.get_instruction.return_value = "system prompt"
    fake._get_game_context.return_value = ""

    groq_response = MagicMock()
    groq_response.choices = [MagicMock()]
    groq_response.choices[0].message.content = groq_content
    fake.groq_dedicated_client.chat.completions.create = AsyncMock(return_value=groq_response)
    fake.groq_fallback_model = "openai/gpt-oss-120b"

    fake.is_exhausted = False
    fake.budget.is_circuit_open.return_value = False
    fake._call_cloud = AsyncMock(return_value=None)
    fake.extract_emotional_moments = AsyncMock(return_value=None)
    return fake


def _entries():
    return [{"speaker": "大肚", "text": "今天在聊排班系統的事"}]


@pytest.mark.asyncio
async def test_groq_empty_content_falls_back_to_cloud_instead_of_crashing():
    """Groq 回空字串 → 不該被當成『已取得結果』，要接著試 _call_cloud。"""
    fake = _make_fake_router(groq_content="")
    fake._call_cloud = AsyncMock(return_value="核心：排班系統吵翻天")

    result = await GeminiRouterContentMixin.generate_slow_summary(fake, _entries())

    fake._call_cloud.assert_awaited_once()
    assert result == "核心：排班系統吵翻天"


@pytest.mark.asyncio
async def test_groq_empty_content_and_cloud_also_empty_returns_none_without_crashing():
    """Groq 空字串 + cloud fallback 也拿不到東西 → 乾淨回 None，不 raise IndexError。"""
    fake = _make_fake_router(groq_content="")
    fake._call_cloud = AsyncMock(return_value=None)

    result = await GeminiRouterContentMixin.generate_slow_summary(fake, _entries())

    assert result is None


@pytest.mark.asyncio
async def test_groq_whitespace_only_content_treated_as_empty():
    """Groq 回純空白字元也該視同空，不能讓 splitlines()[0] 炸在這種邊界上。"""
    fake = _make_fake_router(groq_content="   \n  ")
    fake._call_cloud = AsyncMock(return_value=None)

    result = await GeminiRouterContentMixin.generate_slow_summary(fake, _entries())

    assert result is None


@pytest.mark.asyncio
async def test_groq_real_content_still_works_and_skips_cloud():
    """正常情況（Groq 有內容）不該受影響：不呼叫 _call_cloud，直接回傳摘要。"""
    fake = _make_fake_router(groq_content="核心：今天聊了排班系統")

    result = await GeminiRouterContentMixin.generate_slow_summary(fake, _entries())

    fake._call_cloud.assert_not_awaited()
    assert result == "核心：今天聊了排班系統"
    assert fake.last_slow_summary == "核心：今天聊了排班系統"


@pytest.mark.asyncio
async def test_llm_explicit_skip_still_returns_none():
    """真正的 LLM 主動判斷 SKIP（有內容、明確回 SKIP）行為不變。"""
    fake = _make_fake_router(groq_content="SKIP")

    result = await GeminiRouterContentMixin.generate_slow_summary(fake, _entries())

    assert result is None
    fake._call_cloud.assert_not_awaited()


@pytest.mark.asyncio
async def test_groq_call_uses_reasoning_effort_low_for_gpt_oss_model():
    """gpt-oss 系列推理模型：要傳 reasoning_effort=low 省 token，且 max_tokens 拉到 1500。"""
    fake = _make_fake_router(groq_content="核心：今天聊了排班系統")
    fake.groq_fallback_model = "openai/gpt-oss-120b"

    await GeminiRouterContentMixin.generate_slow_summary(fake, _entries())

    _, kwargs = fake.groq_dedicated_client.chat.completions.create.call_args
    assert kwargs.get("reasoning_effort") == "low"
    assert kwargs.get("max_tokens") == 1500


@pytest.mark.asyncio
async def test_groq_call_omits_reasoning_effort_for_non_gpt_oss_model():
    """非 gpt-oss 模型不該多傳 reasoning_effort（該模型 API 可能不吃這個參數）。"""
    fake = _make_fake_router(groq_content="核心：今天聊了排班系統")
    fake.groq_fallback_model = "llama-3.3-70b-versatile"

    await GeminiRouterContentMixin.generate_slow_summary(fake, _entries())

    _, kwargs = fake.groq_dedicated_client.chat.completions.create.call_args
    assert "reasoning_effort" not in kwargs
    assert kwargs.get("max_tokens") == 1500


@pytest.mark.asyncio
async def test_llm_explicit_skip_logs_warning(caplog):
    """SKIP 目前是 INFO 會被濾掉，改成 WARNING 才看得到；日記整天沒動靜要能查。"""
    fake = _make_fake_router(groq_content="SKIP")

    with caplog.at_level("WARNING"):
        result = await GeminiRouterContentMixin.generate_slow_summary(fake, _entries())

    assert result is None
    assert any("[Diary] LLM 回傳 SKIP" in record.message for record in caplog.records
                if record.levelname == "WARNING")


@pytest.mark.asyncio
async def test_groq_empty_content_logs_warning_with_finish_reason(caplog):
    """Groq 回空內容要留 WARNING（含 finish_reason），不然日記整天沒動靜完全看不出原因。"""
    fake = _make_fake_router(groq_content="")
    fake.groq_dedicated_client.chat.completions.create.return_value.choices[0].finish_reason = "length"
    fake._call_cloud = AsyncMock(return_value="核心：x")

    with caplog.at_level("WARNING"):
        result = await GeminiRouterContentMixin.generate_slow_summary(fake, _entries())

    assert result == "核心：x"
    assert any("Groq 回空內容" in record.message for record in caplog.records
                if record.levelname == "WARNING")
