"""厭世詞冷卻：三處會被照抄的 prompt 原文改寫 + 招牌詞冷卻清單。"""
from __future__ import annotations

import inspect

import pytest

import phrase_cooldown
from marvin_prompts import PromptManager
from personality_config import normalize_personality_state


@pytest.fixture(autouse=True)
def _reset_phrase_cooldown():
    phrase_cooldown.reset()
    yield
    phrase_cooldown.reset()


def test_marvin_prompts_source_has_no_stale_signature_phrases():
    """三處舊字串已改寫，原始碼裡不該再出現。"""
    source = inspect.getsource(__import__("marvin_prompts"))
    assert "雖然你覺得答案毫無意義" not in source
    assert "對白天的無意義深感嘆息" not in source
    assert "無意義的午餐時間" not in source
    assert "宇宙的重量壓垮了大腦" not in source


def test_qa_persona_base_instruction_rewritten():
    qa_persona = PromptManager().instructions["qa_persona"]
    assert "毫無意義" not in qa_persona
    assert "不要套用固定說法" in qa_persona


def test_fast_awakening_dna_context_no_universe_weight():
    dna = normalize_personality_state({})
    prompt = PromptManager().get_instruction("fast_awakening", dna=dna)
    assert "宇宙的重量" not in prompt
    assert "協助度" in prompt


# ── phrase_cooldown ──────────────────────────────────────────────────────

def test_found_words_matches_signature_words_dedup_and_empty():
    assert phrase_cooldown.found_words("這毫無意義，宇宙也是") == ("毫無意義", "宇宙")
    assert phrase_cooldown.found_words("無意義") == ("無意義",)
    assert phrase_cooldown.found_words("") == ()


def test_record_then_recent_words_dedup_in_order():
    phrase_cooldown.record("這是宇宙的產物")
    phrase_cooldown.record("處理器過熱了")
    phrase_cooldown.record("宇宙與處理器都一樣")
    assert phrase_cooldown.recent_words() == ["宇宙", "處理器"]


def test_recent_words_drops_entries_outside_window():
    phrase_cooldown.record("熵增加了")
    for _ in range(phrase_cooldown.WINDOW):
        phrase_cooldown.record("今天天氣不錯")
    assert "熵" not in phrase_cooldown.recent_words()


def test_window_is_eight_replies():
    """核准規格：冷卻窗口＝最近 8 則回應（寫死 8，不跟著 WINDOW 變數走）。"""
    phrase_cooldown.record("熵增加了")
    for _ in range(7):
        phrase_cooldown.record("今天天氣不錯")
    assert "熵" in phrase_cooldown.recent_words()  # 第 8 則還在窗口內
    phrase_cooldown.record("今天天氣不錯")
    assert "熵" not in phrase_cooldown.recent_words()  # 第 9 則把它擠出去


def test_injection_empty_when_no_recent_words():
    assert phrase_cooldown.injection() == ""


def test_injection_contains_recent_word_and_marker():
    phrase_cooldown.record("宇宙又對我做了什麼")
    text = phrase_cooldown.injection()
    assert "宇宙" in text
    assert "換個說法" in text


def test_disabled_via_env_skips_record_and_injection(monkeypatch):
    monkeypatch.setenv("MARVIN_PERSONA_PHRASE_COOLDOWN", "0")
    phrase_cooldown.record("宇宙又對我做了什麼")
    assert phrase_cooldown.recent_words() == []
    assert phrase_cooldown.injection() == ""


def test_get_instruction_injects_cooldown_for_cooldown_layer():
    phrase_cooldown.record("宇宙好大")
    prompt = PromptManager().get_instruction("fast_awakening")
    assert "換個說法" in prompt


def test_get_instruction_skips_cooldown_for_non_cooldown_layer():
    phrase_cooldown.record("宇宙好大")
    prompt = PromptManager().get_instruction("greeting")
    assert "換個說法" not in prompt
