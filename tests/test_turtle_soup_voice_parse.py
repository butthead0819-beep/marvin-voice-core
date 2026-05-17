"""TDD — 海龜湯 voice_parse.classify_intent

意圖分類器，純 regex/keyword，不走 LLM。
測試覆蓋四類：question / surrender / final_answer / ignore。
"""
from __future__ import annotations
import pytest


# ── ignore ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["嗯", "啊", "好", "對啊", "OK", "ok", "嗯啊"])
def test_pure_filler_words_classified_as_ignore(text):
    from game.turtle_soup.voice_parse import classify_intent
    assert classify_intent(text)["intent"] == "ignore"


@pytest.mark.parametrize("text", ["", " ", "  \n", "三", "ab"])
def test_too_short_text_classified_as_ignore(text):
    from game.turtle_soup.voice_parse import classify_intent
    assert classify_intent(text)["intent"] == "ignore"


# ── surrender ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "我投降",
    "投降啦",
    "不玩了",
    "我放棄",
    "放棄好了",
    "我認輸",
    "我棄權",
])
def test_surrender_phrases_classified_as_surrender(text):
    from game.turtle_soup.voice_parse import classify_intent
    result = classify_intent(text)
    assert result["intent"] == "surrender"
    assert result["payload"] == text


# ── final_answer ────────────────────────────────────────────────────────────

def test_final_answer_with_answer_prefix():
    from game.turtle_soup.voice_parse import classify_intent
    result = classify_intent("答案是他是侏儒按不到 22 樓按鈕")
    assert result["intent"] == "final_answer"
    assert result["payload"] == "他是侏儒按不到 22 樓按鈕"


def test_final_answer_with_i_think_prefix():
    from game.turtle_soup.voice_parse import classify_intent
    result = classify_intent("我認為答案是男子身高不夠")
    assert result["intent"] == "final_answer"
    assert "男子身高不夠" in result["payload"]


def test_final_answer_strips_prefix_phrases():
    from game.turtle_soup.voice_parse import classify_intent
    for prefix in ["答案是", "我認為答案是", "我覺得是", "我猜是"]:
        result = classify_intent(f"{prefix}他構不到按鈕")
        assert result["intent"] == "final_answer"
        assert result["payload"] == "他構不到按鈕"


# ── question ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "他是侏儒嗎？",
    "電梯壞了嗎",
    "他害怕高處嗎？",
    "22 樓有什麼特別的東西",
    "他每天都這樣做嗎？",
])
def test_normal_questions_classified_as_question(text):
    from game.turtle_soup.voice_parse import classify_intent
    result = classify_intent(text)
    assert result["intent"] == "question"
    assert result["payload"] == text


# ── 邊界 / 優先順序 ─────────────────────────────────────────────────────────

def test_surrender_takes_priority_over_question():
    """若同時含 surrender keyword 和問句結構，視為 surrender。"""
    from game.turtle_soup.voice_parse import classify_intent
    result = classify_intent("我投降了，要不要告訴我答案？")
    assert result["intent"] == "surrender"


def test_final_answer_only_if_at_start():
    """『答案是』不在開頭時，不視為 final_answer。"""
    from game.turtle_soup.voice_parse import classify_intent
    result = classify_intent("他有什麼問題嗎？答案是不是身高")
    assert result["intent"] == "question"


def test_whitespace_stripped_before_classify():
    from game.turtle_soup.voice_parse import classify_intent
    result = classify_intent("   他是侏儒嗎？   ")
    assert result["intent"] == "question"
    assert result["payload"] == "他是侏儒嗎？"
