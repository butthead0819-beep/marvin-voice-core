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


# ── question（必須以「請問」等明確前綴開頭）────────────────────────────────

@pytest.mark.parametrize("text,expected_payload", [
    ("請問他是侏儒嗎？", "他是侏儒嗎？"),
    ("請問電梯壞了嗎", "電梯壞了嗎"),
    ("我想問他害怕高處嗎", "他害怕高處嗎"),
    ("問一下他是不是矮個子", "他是不是矮個子"),
    ("我問你他每天都這樣嗎", "他每天都這樣嗎"),
    ("我可以問他是男生嗎", "他是男生嗎"),
])
def test_question_with_prefix_classified_as_question(text, expected_payload):
    from game.turtle_soup.voice_parse import classify_intent
    result = classify_intent(text)
    assert result["intent"] == "question"
    assert result["payload"] == expected_payload


# ── discussion（沒有「請問」前綴的句子都歸 discussion，不送 LLM）──────────────

@pytest.mark.parametrize("text", [
    "他是侏儒嗎？",                  # 沒前綴，雖然像問句也歸 discussion
    "電梯壞了嗎",
    "他害怕高處嗎？",
    "我覺得他怕高",                  # 玩家內部討論
    "等一下我想一下",
    "可能跟身高有關吧",
    "我不知道耶",
])
def test_no_question_prefix_classified_as_discussion(text):
    """玩家自然對話、討論、推理 → discussion，不送 LLM。"""
    from game.turtle_soup.voice_parse import classify_intent
    result = classify_intent(text)
    assert result["intent"] == "discussion"


# ── 邊界 / 優先順序 ─────────────────────────────────────────────────────────

def test_surrender_takes_priority_over_question_prefix():
    """surrender 優先於 question prefix。"""
    from game.turtle_soup.voice_parse import classify_intent
    result = classify_intent("請問我可以投降嗎")
    assert result["intent"] == "surrender"


def test_final_answer_takes_priority_over_question_prefix():
    """final_answer 優先於 question prefix。"""
    from game.turtle_soup.voice_parse import classify_intent
    result = classify_intent("答案是他是侏儒")
    assert result["intent"] == "final_answer"


def test_question_prefix_must_be_at_start():
    """「請問」不在開頭時，仍視為 discussion。"""
    from game.turtle_soup.voice_parse import classify_intent
    result = classify_intent("我們先想一下請問他是不是侏儒")
    assert result["intent"] == "discussion"


def test_whitespace_stripped_before_classify():
    from game.turtle_soup.voice_parse import classify_intent
    result = classify_intent("   請問他是侏儒嗎？   ")
    assert result["intent"] == "question"
    assert result["payload"] == "他是侏儒嗎？"


def test_question_prefix_strips_trailing_punctuation():
    """前綴後可能有逗號/句號，要剝掉再送 LLM。"""
    from game.turtle_soup.voice_parse import classify_intent
    result = classify_intent("請問，他是不是侏儒")
    assert result["intent"] == "question"
    assert result["payload"] == "他是不是侏儒"
