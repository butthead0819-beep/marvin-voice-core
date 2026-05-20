"""TDD: 英文 Marvin prefix STT hallucination 降到 Track B（llm_verify）。

2026-05-20 真實 prod 觀察：v4 prompt 把英文 Marvin 翻譯為馬文視為喚醒 →
STT 在中文語音前 hallucinate「Marvin,」前綴 → cleaner 直接信 → false wake。

修法：pre_filter_speech 內 _FAST_RE 命中時，若 matched_word 是英文 Marvin
變體（marvin/marv/mavin/hey marvin/oh marvin）且後續 rest 無真英文內容
（≥3 連續英文字母），降到 llm_verify 讓 LLM 判 intent。

真實 false wake 案例（2026-05-20 15:32-16:04）：
  - 'Marvin, 李宗盛'
  - 'Marvin, 我敬甜了嗎?'
  - 'Marvin, 李宗盛, 李宗盛'
  - 'Marvin'
  - 'Marvin, 馬文, 艾馬文, 艾馬文雅'
"""
from __future__ import annotations

import pytest

from wake_detector import pre_filter_speech


# ── 1. 英文 Marvin + 純中文後綴 → 必須 demote ─────────────────────────────

@pytest.mark.parametrize("raw", [
    "Marvin, 李宗盛",
    "Marvin, 我敬甜了嗎?",
    "Marvin, 李宗盛, 李宗盛",
    "Marvin",                          # 孤立 Marvin（無 context）
    "Marvin, 馬文, 艾馬文, 艾馬文雅",
    "marvin 怎麼樣",
    "Marv, 你好嗎",                    # 變體 Marv
    "Mavin 沒事吧",
    "hey marvin 怎麼了",               # 含 hey 但後續純中文
])
def test_english_marvin_with_chinese_only_demotes_to_llm_verify(raw):
    """英文 Marvin 變體後續無 ≥3 letters 英文內容 → 降到 llm_verify。"""
    result = pre_filter_speech(raw)
    assert result["action"] == "llm_verify", \
        f"raw={raw!r} 該降到 llm_verify，實際: {result}"


# ── 2. 英文 Marvin + 英文後綴 → 保留 fast_intervene（真英文呼叫）─────────

@pytest.mark.parametrize("raw", [
    "Marvin, are you OK",
    "Marvin, hello world",
    "Marvin play music now",
    "Marvin tell me something",
    "hey marvin what is this",
    "Marvin, OK go play music",        # 短英文 + 後續英文
    "Marvin, 李宗盛 sings well",       # 中英混合，有英文支援
])
def test_english_marvin_with_real_english_keeps_fast_intervene(raw):
    """真英文呼叫（後續含 ≥3 letters 英文詞）→ fast_intervene 不變。"""
    result = pre_filter_speech(raw)
    assert result["action"] == "fast_intervene", \
        f"raw={raw!r} 該保留 fast_intervene，實際: {result}"


# ── 3. 中文 wake 不受影響 ─────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected_action", [
    ("馬文", "fast_intervene"),
    ("嗨馬文 你好", "fast_intervene"),
    ("馬文 你在嗎", "fast_intervene"),
    ("艾馬文 怎麼了", "fast_intervene"),
    ("嗨馬文 咱說什麼詞場", "fast_intervene"),
    ("馬文播放周杰倫", "fast_intervene"),
])
def test_chinese_marvin_wakes_unaffected(raw, expected_action):
    """中文 wake 不該被英文 demotion 邏輯影響。"""
    result = pre_filter_speech(raw)
    assert result["action"] == expected_action, \
        f"raw={raw!r} 預期 {expected_action}，實際: {result}"


# ── 4. Noise prefix 剝離後仍正確 ─────────────────────────────────────────

def test_noise_prefix_then_marvin_chinese_still_demotes():
    """STT 在前綴黏 noise + 後續英文 Marvin + 中文 → 應 demote。"""
    # 「Yeah, Marvin, 李宗盛」剝完 Yeah, 後變「Marvin, 李宗盛」
    result = pre_filter_speech("Yeah, Marvin, 李宗盛")
    assert result["action"] == "llm_verify"


def test_noise_prefix_then_marvin_chinese_no_match():
    """STT 在前綴黏 noise + 純中文 wake（無英文 Marvin）→ 中文 fast_intervene。"""
    result = pre_filter_speech("On, 馬文 你好")
    assert result["action"] == "fast_intervene"


# ── 5. 邊界 case ─────────────────────────────────────────────────────────

def test_marvin_with_exactly_3_letters_english_keeps_fast():
    """3 letters 是 threshold 邊界，剛好滿足。"""
    result = pre_filter_speech("Marvin, hey there")
    assert result["action"] == "fast_intervene"


def test_marvin_with_2_letter_english_demotes():
    """2 letters 不滿足 ≥3 threshold，視為非真英文，demote。"""
    result = pre_filter_speech("Marvin, ok 啦")
    assert result["action"] == "llm_verify"


def test_drop_action_unchanged():
    """無喚醒詞 → drop（與 fix 無關但確保沒破壞）。"""
    result = pre_filter_speech("今天天氣真好")
    assert result["action"] == "drop"
