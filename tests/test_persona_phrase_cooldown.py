"""厭世詞冷卻：三處會被照抄的 prompt 原文改寫 + 招牌詞冷卻清單。"""
from __future__ import annotations

import inspect

import pytest

from marvin_prompts import PromptManager
from personality_config import normalize_personality_state


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
    dna = normalize_personality_state({"toxicity": 5})
    prompt = PromptManager().get_instruction("fast_awakening", dna=dna)
    assert "宇宙的重量" not in prompt
    assert "提不起任何勁" in prompt
