"""TDD — 海龜湯 LLM judge

驗證：
- judge_question 三層 fallback（Cerebras → Groq → Gemini → fallback string）
- 三層全掛回 irrelevant + 系統忙線 narration
- post_filter_narration 移除洩底關鍵詞
- judge_final_guess 判定接受條件（cover key_facts[0] 與 [1]）
"""
from __future__ import annotations
import pytest
from unittest.mock import AsyncMock, patch


SURFACE = "男子住在 22 樓..."
TRUTH = "男子是侏儒，按不到 22 樓按鈕。"
KEY_FACTS = [
    "男子是侏儒",
    "電梯按鈕高度問題",
    "18 樓是最高可按",
    "早上下樓沒問題",
    "有人陪伴可直達 22 樓",
]
LEAK_KEYWORDS = ["侏儒", "矮", "按鈕", "夠不到"]


# ── judge_question fallback chain ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cerebras_succeeds_returns_immediately():
    from game.turtle_soup import llm_judge

    with patch.object(llm_judge, "_call_cerebras", new=AsyncMock(return_value={
        "verdict": "yes", "narration": "你抓到了",
    })), patch.object(llm_judge, "_call_groq", new=AsyncMock()) as groq, \
         patch.object(llm_judge, "_call_gemini", new=AsyncMock()) as gemini:
        result = await llm_judge.judge_question(SURFACE, TRUTH, "他是侏儒嗎？", [], LEAK_KEYWORDS)

    assert result["verdict"] == "yes"
    assert result["_provider"] == "Cerebras"
    groq.assert_not_called()
    gemini.assert_not_called()


@pytest.mark.asyncio
async def test_cerebras_fails_groq_succeeds():
    from game.turtle_soup import llm_judge

    with patch.object(llm_judge, "_call_cerebras", new=AsyncMock(return_value=None)), \
         patch.object(llm_judge, "_call_groq", new=AsyncMock(return_value={
             "verdict": "no", "narration": "想太多",
         })), \
         patch.object(llm_judge, "_call_gemini", new=AsyncMock()) as gemini:
        result = await llm_judge.judge_question(SURFACE, TRUTH, "電梯壞了嗎？", [], LEAK_KEYWORDS)

    assert result["verdict"] == "no"
    assert result["_provider"] == "Groq"
    gemini.assert_not_called()


@pytest.mark.asyncio
async def test_cerebras_groq_fail_gemini_succeeds():
    from game.turtle_soup import llm_judge

    with patch.object(llm_judge, "_call_cerebras", new=AsyncMock(return_value=None)), \
         patch.object(llm_judge, "_call_groq", new=AsyncMock(return_value=None)), \
         patch.object(llm_judge, "_call_gemini", new=AsyncMock(return_value={
             "verdict": "irrelevant", "narration": "離題了",
         })):
        result = await llm_judge.judge_question(SURFACE, TRUTH, "他穿紅色嗎？", [], LEAK_KEYWORDS)

    assert result["verdict"] == "irrelevant"
    assert result["_provider"] == "Gemini"


@pytest.mark.asyncio
async def test_all_three_fail_returns_safe_fallback():
    from game.turtle_soup import llm_judge

    with patch.object(llm_judge, "_call_cerebras", new=AsyncMock(return_value=None)), \
         patch.object(llm_judge, "_call_groq", new=AsyncMock(return_value=None)), \
         patch.object(llm_judge, "_call_gemini", new=AsyncMock(return_value=None)):
        result = await llm_judge.judge_question(SURFACE, TRUTH, "anything", [], LEAK_KEYWORDS)

    assert result["verdict"] == "irrelevant"
    assert result["_provider"] == "fallback"
    assert "請再" in result["narration"] or "再問" in result["narration"]


@pytest.mark.asyncio
async def test_invalid_verdict_treated_as_failure_and_falls_through():
    """LLM 回傳非預期 verdict（'maybe'）視為失敗，try 下一層。"""
    from game.turtle_soup import llm_judge

    with patch.object(llm_judge, "_call_cerebras", new=AsyncMock(return_value={
        "verdict": "maybe", "narration": "x",
    })), patch.object(llm_judge, "_call_groq", new=AsyncMock(return_value={
        "verdict": "yes", "narration": "ok",
    })):
        result = await llm_judge.judge_question(SURFACE, TRUTH, "q", [], LEAK_KEYWORDS)

    assert result["_provider"] == "Groq"
    assert result["verdict"] == "yes"


# ── narration post-filter（洩底封口）─────────────────────────────────────────

def test_post_filter_removes_leak_when_question_does_not_contain_keyword():
    """narration 含『侏儒』但 question 不含 → 改寫成通用回應。"""
    from game.turtle_soup.llm_judge import post_filter_narration

    filtered = post_filter_narration(
        narration="沒錯，他是侏儒",
        question="他害怕電梯嗎？",
        verdict="yes",
        leak_keywords=["侏儒", "按鈕"],
    )
    assert "侏儒" not in filtered


def test_post_filter_keeps_narration_when_question_contains_keyword():
    """玩家問題已含『侏儒』時，narration 可以提及（玩家自己說的）。"""
    from game.turtle_soup.llm_judge import post_filter_narration

    filtered = post_filter_narration(
        narration="沒錯，他是侏儒",
        question="他是侏儒嗎？",
        verdict="yes",
        leak_keywords=["侏儒"],
    )
    assert filtered == "沒錯，他是侏儒"


def test_post_filter_picks_appropriate_replacement_per_verdict():
    """yes/no/irrelevant 各有對應的兜底回應。"""
    from game.turtle_soup.llm_judge import post_filter_narration

    for v in ("yes", "no", "irrelevant"):
        out = post_filter_narration(
            narration="他構不到按鈕",  # 含洩底詞
            question="他害怕電梯嗎？",   # 不含
            verdict=v,
            leak_keywords=["構不到", "按鈕"],
        )
        for kw in ("構不到", "按鈕"):
            assert kw not in out


# ── judge_final_guess 接受條件 ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_final_guess_accepted_when_two_core_facts_covered():
    """key_facts[0] 和 [1] 都命中 → accepted。"""
    from game.turtle_soup import llm_judge

    with patch.object(llm_judge, "_call_cerebras_final", new=AsyncMock(return_value={
        "covered_facts": [0, 1],
        "narration": "你想通了",
    })):
        result = await llm_judge.judge_final_guess(
            SURFACE, TRUTH, KEY_FACTS, "他是侏儒按不到按鈕",
        )
    assert result["accepted"] is True
    assert 0 in result["covered_facts"]
    assert 1 in result["covered_facts"]


@pytest.mark.asyncio
async def test_final_guess_rejected_when_only_one_core_fact_covered():
    """只命中 key_facts[0] 但缺 [1] → rejected。"""
    from game.turtle_soup import llm_judge

    with patch.object(llm_judge, "_call_cerebras_final", new=AsyncMock(return_value={
        "covered_facts": [0],
        "narration": "差一點點",
    })):
        result = await llm_judge.judge_final_guess(
            SURFACE, TRUTH, KEY_FACTS, "他是侏儒",
        )
    assert result["accepted"] is False


@pytest.mark.asyncio
async def test_final_guess_rejected_when_zero_core_facts():
    from game.turtle_soup import llm_judge

    with patch.object(llm_judge, "_call_cerebras_final", new=AsyncMock(return_value={
        "covered_facts": [],
        "narration": "完全不對",
    })):
        result = await llm_judge.judge_final_guess(
            SURFACE, TRUTH, KEY_FACTS, "他害怕電梯",
        )
    assert result["accepted"] is False


@pytest.mark.asyncio
async def test_final_guess_all_layers_fail_returns_rejected():
    from game.turtle_soup import llm_judge

    with patch.object(llm_judge, "_call_cerebras_final", new=AsyncMock(return_value=None)), \
         patch.object(llm_judge, "_call_groq_final", new=AsyncMock(return_value=None)), \
         patch.object(llm_judge, "_call_gemini_final", new=AsyncMock(return_value=None)):
        result = await llm_judge.judge_final_guess(SURFACE, TRUTH, KEY_FACTS, "x")
    assert result["accepted"] is False
    assert result["_provider"] == "fallback"
