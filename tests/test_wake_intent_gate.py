"""Wake-after intent presence gate（避免 Marvin 在錯時機亂回答）。

flow context：cleaner 已認定 is_wake，IntentBus 也沒接到 → 進 Marvin 主 LLM
fall-through。這層 gate 是「raw 有沒有實質指令訊號」純 code 判定：
有問句/指令動詞/足夠長度 → 放行；只有 filler/短應答 → silent（不打 LLM）。
"""
from __future__ import annotations

import pytest

from wake_intent_gate import has_intent_signal


# ── 短應答 / filler → 不打 LLM ───────────────────────────────────────────────

@pytest.mark.parametrize("q", [
    "嗯", "啊", "喔", "欸", "呃", "誒",
    "嗯嗯", "啊啊", "喔喔",
    "對", "對啊", "對對", "好", "好啊", "好的",
    "沒事", "沒有",
])
def test_filler_and_short_ack_blocked(q):
    assert has_intent_signal(q) is False, f"short ack '{q}' should be blocked"


def test_empty_blocked():
    assert has_intent_signal("") is False
    assert has_intent_signal("   ") is False


def test_punctuation_only_blocked():
    assert has_intent_signal("。。。") is False
    assert has_intent_signal("...") is False


# ── 問句結構 → 放行 ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("q", [
    "你好嗎",
    "現在幾點",
    "為什麼",
    "什麼意思",
    "怎麼辦",
    "誰啊",
    "哪裡",
    "你在嗎",
    "可以嗎？",
    "好嗎？",
])
def test_questions_pass(q):
    assert has_intent_signal(q) is True, f"question '{q}' should pass"


# ── 指令動詞 → 放行 ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("q", [
    "幫我查天氣",
    "告訴我",
    "教我寫程式",
    "翻譯這句",
    "解釋一下",
    "介紹自己",
    "請你說",
])
def test_imperatives_pass(q):
    assert has_intent_signal(q) is True, f"imperative '{q}' should pass"


# ── 足夠長度（非 filler）→ 放行 ─────────────────────────────────────────────

@pytest.mark.parametrize("q", [
    "我覺得今天天氣不錯",   # 陳述句，但夠長 → 可能是對話內容
    "剛剛那個很有趣",
])
def test_long_statements_pass(q):
    assert has_intent_signal(q) is True, f"long statement '{q}' should pass"


# ── 邊界：含問號的短語也算問句 ────────────────────────────────────────────────

def test_short_with_question_mark_passes():
    assert has_intent_signal("好?") is True
    assert has_intent_signal("嗎？") is True


# ── 邊界：完全是 wake 詞（cleaner 注入後）→ 不算指令訊號 ────────────────────

def test_pure_wake_word_blocked():
    """cleaner 注入「馬文」的句子，剝掉 wake 後沒剩什麼 → 應該被擋。"""
    assert has_intent_signal("馬文") is False
    assert has_intent_signal("馬文。") is False
