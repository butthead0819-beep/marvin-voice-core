"""Marmo persona in CHARACTER_PRESETS — dual-speak PoC 第一塊內容層。

驗證：
  - "marmo" preset 存在、display_name / persona_tag / voice_summary 正確
  - axes 值反映「代用戶打斷者」的功能定位（directness/sarcasm 高、compassion/resignation 低）
  - build_personality_prompt_context("marmo") 觸發 directness 高 + verbosity 低 + sarcasm 高
    三條 flavor 行（既有 logic，新人格自然吃到）
  - 不影響 marvin 的 prompt 產出（regression）
"""
from __future__ import annotations

from personality_config import (
    CHARACTER_PRESETS,
    build_personality_prompt_context,
    get_preset,
)


def test_marmo_preset_exists():
    assert "marmo" in CHARACTER_PRESETS
    preset = CHARACTER_PRESETS["marmo"]
    assert preset["display_name"]
    assert preset["persona_tag"]
    assert preset["voice_summary"]


def test_marmo_axes_reflect_interrupter_role():
    """軸值對應 design doc：代用戶打斷者 = 高直接 + 高冷諷 + 低同情 + 低無奈。"""
    axes = CHARACTER_PRESETS["marmo"]["axes"]
    assert axes["directness"] >= 0.90, "打斷者必須直接，第一句就丟答案"
    assert axes["sarcasm"] >= 0.90, "反擊味要強"
    assert axes["compassion"] <= 0.20, "對 Marvin 沒耐心，不安慰"
    assert axes["resignation"] <= 0.20, "不認命，會主動戳"
    assert axes["oppression"] <= 0.30, "不壓抑，跟 Marvin 厭世剛好相反"
    assert axes["verbosity"] <= 0.30, "短句、不囉嗦"


def test_marmo_prompt_context_triggers_expected_flavor_lines():
    """既有 build_personality_prompt_context 有 4 條 conditional flavor，
    Marmo 應觸發 directness/verbosity/sarcasm 三條，不觸發 compassion 那條。"""
    prompt = build_personality_prompt_context({"character": "marmo"})
    assert "直接度高" in prompt
    assert "話量低" in prompt
    assert "冷諷較高" in prompt
    assert "同情較高" not in prompt
    # display_name 也應出現
    assert "馬末" in prompt or CHARACTER_PRESETS["marmo"]["display_name"] in prompt


def test_marvin_prompt_unchanged_after_marmo_added():
    """Regression：加 marmo entry 不應改變 marvin 的 prompt 輸出。"""
    prompt = build_personality_prompt_context({"character": "marvin"})
    # Marvin 的特徵 flavor：話量低 + 直接度高（sarcasm 0.45 不觸發冷諷較高）
    assert "馬文" in prompt
    assert "話量低" in prompt
    assert "直接度高" in prompt
    assert "冷諷較高" not in prompt, "Marvin sarcasm 0.45 不該觸發冷諷較高"


def test_get_preset_marmo_returns_independent_copy():
    """get_preset 用 deepcopy，回出的 dict 改動不該影響全域。"""
    p1 = get_preset("marmo")
    p1["axes"]["sarcasm"] = 0.0
    p2 = get_preset("marmo")
    assert p2["axes"]["sarcasm"] >= 0.90
