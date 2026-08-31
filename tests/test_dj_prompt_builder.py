"""TDD: DJ Unified Prompt Builder 測試。

驗證：
1. 統整所有 DJ Prompt（interjection, radio_now_playing, stream_now_playing）。
2. 保證核心護欄（防幻覺、掛名護欄、字數上限、機器人視角、禁詞、Threads 生活幽默）。
3. 支援取得統一規則規範清單。
"""
from __future__ import annotations

import pytest
from dj_prompt_builder import (
    build_dj_interjection_prompt,
    build_radio_now_playing_prompt,
    build_stream_now_playing_prompt,
    get_dj_unified_rules,
)


def test_build_dj_interjection_prompt_contains_all_guards():
    """驗證 crossfade 串場 prompt 包含所有必備規則與護欄。"""
    ctx = "歌曲：周杰倫 - 夜曲\n點播者：大肚"
    prompt = build_dj_interjection_prompt(ctx)
    assert "DJ Marvin" in prompt
    assert ctx in prompt
    # 核心護欄驗證
    assert "長度硬上限" in prompt or "45-55" in prompt
    assert "機器人" in prompt
    assert "掛名只能照脈絡" in prompt
    assert "不考驗聽眾記憶" in prompt
    assert "Threads" in prompt or "生活小幽默" in prompt
    assert "只輸出台詞" in prompt


def test_build_radio_now_playing_prompt():
    """驗證電台即時報幕 prompt 格式與字數約束。"""
    ctx = "周杰倫 - 晴天 (2004)"
    prompt = build_radio_now_playing_prompt(ctx)
    assert "電台 DJ" in prompt
    assert "晴天" in prompt
    assert "20-23" in prompt or "7 秒" in prompt


def test_build_stream_now_playing_prompt():
    """驗證直播點播報幕 prompt。"""
    ctx = "周杰倫 - 稻香 (點播者：Alice)"
    prompt = build_stream_now_playing_prompt(ctx)
    assert "電台 DJ" in prompt
    assert "點播" in prompt
    assert "稻香" in prompt


def test_get_dj_unified_rules():
    """驗證統一規範清單結構。"""
    rules = get_dj_unified_rules()
    assert "length_rule" in rules
    assert "material_guard" in rules
    assert "naming_guard" in rules
    assert "memory_claim_guard" in rules
    assert "robot_pov_guard" in rules
    assert "forbidden_phrases" in rules
    assert len(rules["forbidden_phrases"]) >= 5
