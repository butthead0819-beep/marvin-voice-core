"""TDD — hint_generator：3-layer fallback + JSON validation + leak filter。"""
from __future__ import annotations
import pytest
from unittest.mock import AsyncMock, patch


SURFACE = "男子住在 22 樓..."
TRUTH = "男子是侏儒，按不到 22 樓按鈕。"
KEY_FACTS = ["男子是侏儒", "電梯按鈕高度問題"]
LEAK_KEYWORDS = ["侏儒", "矮", "按鈕"]


def _good_response():
    return {
        "direct": "想想他的身體有什麼特別",
        "two_dimensional": "為什麼早上能到 1 樓晚上不行",
        "three_dimensional": "有人陪同就行獨自不行—這是什麼限制",
    }


# ── 3-layer fallback ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cerebras_succeeds_returns_immediately():
    from game.turtle_soup import hint_generator

    with patch.object(hint_generator, "_call_cerebras",
                      new=AsyncMock(return_value=_good_response())), \
         patch.object(hint_generator, "_call_groq", new=AsyncMock()) as groq, \
         patch.object(hint_generator, "_call_gemini", new=AsyncMock()) as gemini:
        result = await hint_generator.generate_hint_tiers(
            SURFACE, TRUTH, KEY_FACTS, LEAK_KEYWORDS,
        )

    assert result["_provider"] == "Cerebras"
    assert result["direct"]
    assert result["two_dimensional"]
    assert result["three_dimensional"]
    groq.assert_not_called()
    gemini.assert_not_called()


@pytest.mark.asyncio
async def test_falls_through_to_groq():
    from game.turtle_soup import hint_generator
    with patch.object(hint_generator, "_call_cerebras", new=AsyncMock(return_value=None)), \
         patch.object(hint_generator, "_call_groq", new=AsyncMock(return_value=_good_response())), \
         patch.object(hint_generator, "_call_gemini", new=AsyncMock()) as gemini:
        result = await hint_generator.generate_hint_tiers(SURFACE, TRUTH, KEY_FACTS, LEAK_KEYWORDS)
    assert result["_provider"] == "Groq"
    gemini.assert_not_called()


@pytest.mark.asyncio
async def test_falls_through_to_gemini():
    from game.turtle_soup import hint_generator
    with patch.object(hint_generator, "_call_cerebras", new=AsyncMock(return_value=None)), \
         patch.object(hint_generator, "_call_groq", new=AsyncMock(return_value=None)), \
         patch.object(hint_generator, "_call_gemini", new=AsyncMock(return_value=_good_response())):
        result = await hint_generator.generate_hint_tiers(SURFACE, TRUTH, KEY_FACTS, LEAK_KEYWORDS)
    assert result["_provider"] == "Gemini"


@pytest.mark.asyncio
async def test_all_three_fail_returns_safe_fallback():
    from game.turtle_soup import hint_generator
    with patch.object(hint_generator, "_call_cerebras", new=AsyncMock(return_value=None)), \
         patch.object(hint_generator, "_call_groq", new=AsyncMock(return_value=None)), \
         patch.object(hint_generator, "_call_gemini", new=AsyncMock(return_value=None)):
        result = await hint_generator.generate_hint_tiers(SURFACE, TRUTH, KEY_FACTS, LEAK_KEYWORDS)
    assert result["_provider"] == "fallback"
    assert result["direct"] == ""
    assert result["two_dimensional"] == ""
    assert result["three_dimensional"] == ""


# ── JSON validation ──────────────────────────────────────────────────────────

def test_validate_accepts_well_formed():
    from game.turtle_soup.hint_generator import _validate
    assert _validate(_good_response()) is not None


def test_validate_rejects_missing_key():
    from game.turtle_soup.hint_generator import _validate
    assert _validate({"direct": "a", "two_dimensional": "b"}) is None


def test_validate_rejects_empty_string():
    from game.turtle_soup.hint_generator import _validate
    bad = _good_response()
    bad["direct"] = ""
    assert _validate(bad) is None


def test_validate_rejects_non_dict():
    from game.turtle_soup.hint_generator import _validate
    assert _validate("not a dict") is None
    assert _validate(None) is None
    assert _validate([1, 2, 3]) is None


def test_validate_strips_whitespace():
    from game.turtle_soup.hint_generator import _validate
    result = _validate({
        "direct": "  hello  ",
        "two_dimensional": "world",
        "three_dimensional": "!",
    })
    assert result["direct"] == "hello"


# ── leak filter ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_leak_filter_marks_hints_with_keywords():
    """若 LLM 不小心吐出洩底詞，加上 ⚠[LEAK:KW] 警告（不自動改寫）。"""
    from game.turtle_soup import hint_generator
    leaky = {
        "direct": "他是侏儒",  # 直接洩底
        "two_dimensional": "為什麼按不到",  # 含洩底詞「按不到」？看 leak_keywords
        "three_dimensional": "想想看",
    }
    with patch.object(hint_generator, "_call_cerebras",
                      new=AsyncMock(return_value=leaky)):
        result = await hint_generator.generate_hint_tiers(
            SURFACE, TRUTH, KEY_FACTS, ["侏儒", "按不到"],
        )
    assert "⚠[LEAK" in result["direct"]
    assert "侏儒" in result["direct"]  # 原文保留供作者參考
    assert "⚠[LEAK" in result["two_dimensional"]
    # 第三條乾淨，不該被標記
    assert "⚠" not in result["three_dimensional"]


@pytest.mark.asyncio
async def test_leak_filter_skips_when_no_keywords_in_hint():
    from game.turtle_soup import hint_generator
    with patch.object(hint_generator, "_call_cerebras",
                      new=AsyncMock(return_value=_good_response())):
        result = await hint_generator.generate_hint_tiers(
            SURFACE, TRUTH, KEY_FACTS, LEAK_KEYWORDS,
        )
    assert "⚠" not in result["direct"]
    assert "⚠" not in result["two_dimensional"]
    assert "⚠" not in result["three_dimensional"]
