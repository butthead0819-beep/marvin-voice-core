"""口白純觀察者語氣 + 拔除 emotional_highlight mode + 選歌理由新句型（2026-10-09 使用者定）。"""
from __future__ import annotations

import inspect

import yaml

import dj_narration_orchestrator
import dj_topic_selector
from cogs.music_cog import MusicCog
from dj_narration_orchestrator import format_reason_line
from dj_prompt_builder import DJ_MEMORY_CLAIM_GUARD, build_dj_interjection_prompt
from dj_topic_selector import MODE_WEIGHTS, select_mode


def test_format_reason_line_basic():
    assert format_reason_line("大肚", "出嫁", "你們一起聽過這首") == \
        "推薦理由：大肚會喜歡這首《出嫁》，理由是你們一起聽過這首"


def test_format_reason_line_empty_who_defaults_to_dajia():
    assert format_reason_line("", "出嫁", "理由") == \
        "推薦理由：大家會喜歡這首《出嫁》，理由是理由"


def test_prompt_has_no_this_song_for_you_phrase():
    prompt = build_dj_interjection_prompt("x")
    assert "這首給你" not in prompt


def test_prompt_contains_no_first_person_rule():
    prompt = build_dj_interjection_prompt("x")
    assert "不准有「我」" in prompt


def test_memory_claim_guard_drops_old_hedge_language():
    assert "比較少聽" not in DJ_MEMORY_CLAIM_GUARD
    assert "希望" not in DJ_MEMORY_CLAIM_GUARD


def test_autopilot_phrase_templates_have_no_banned_words():
    with open("personas/dj_templates.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    banned = ("挖", "馬文", "我")
    for pool_name, phrases in data["autopilot_phrases"].items():
        for phrase in phrases:
            for word in banned:
                assert word not in phrase, f"{pool_name} 樣版含禁用詞「{word}」：{phrase}"


def test_discovery_reason_has_no_dig_word():
    reason = MusicCog._autopilot_pick_reason({"_lane": "discovery", "_spotlight": "Alice"})
    assert "挖" not in reason


def test_emotional_highlight_mode_removed():
    assert "emotional_highlight" not in MODE_WEIGHTS
    assert "emotional_highlight" not in dj_narration_orchestrator.MODES


def test_select_mode_signature_has_no_emotional_highlights_param():
    params = inspect.signature(select_mode).parameters
    assert "emotional_highlights" not in params
