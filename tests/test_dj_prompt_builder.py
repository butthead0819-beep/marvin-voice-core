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
    build_dj_joke_interjection_prompt,
    build_radio_now_playing_prompt,
    build_stream_now_playing_prompt,
    get_dj_unified_rules,
)
from joke_examples import JOKE_EXAMPLES


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


def test_build_dj_joke_interjection_prompt_contains_joke_style_and_safety_guards():
    """厭世冷笑話插播 prompt：保留防幻覺/掛名/不考驗記憶等安全護欄，但改用厭世收尾
    風格取代平常「不諷刺不憂鬱」的暖場調性，並共用 joke_examples 範例庫。"""
    ctx = "歌曲：周杰倫 - 夜曲"
    prompt = build_dj_joke_interjection_prompt(ctx)
    assert "DJ Marvin" in prompt
    assert ctx in prompt
    assert "厭世" in prompt
    assert "宇宙" in prompt
    assert "掛名只能照脈絡" in prompt
    assert "不考驗聽眾記憶" in prompt
    assert "45-55" in prompt
    assert "只輸出台詞" in prompt
    # 範例庫共用（至少要看到一則範例，不是各自維護一份）
    assert any(example in prompt for example in JOKE_EXAMPLES)


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


# ── 聽覺放大鏡導聆（docs/PLAN_audiophile_music_tour.md Phase 1）────────────────

def test_build_audiophile_guide_prompt():
    """導聆 prompt：當 grounded 呼叫的 system_instruction 用（見 audiophile_fetcher）。
    要鎖住：90-110 字長度、聽覺線索（聲場/樂器/突變）、多維度切入角度（幕後/唱腔/焦點）、
    禁假文青套話與八股反轉句型（沿用 FORBIDDEN_DJ_PHRASES 單一來源）、只輸出台詞，
    以及零幻覺的兩道門：要求 Google 查證、查不到只回「無」（接 grounded_answer 的 L1 拒答 guard）。"""
    from dj_prompt_builder import FORBIDDEN_DJ_PHRASES, build_audiophile_guide_prompt

    label = "周杰倫 - 雙截棍"
    prompt = build_audiophile_guide_prompt(label)
    assert label in prompt
    # 長度規範
    assert "90-110" in prompt
    # 聽覺線索引導
    for cue in ("聲場", "樂器", "突變"):
        assert cue in prompt, cue
    # 破除既定印象與聽覺錨點
    assert "破除" in prompt
    assert "聽覺錨點" in prompt
    # 多維度切入視角引導（幕後軼事、唱腔、聽覺焦點）
    assert "幕後" in prompt
    assert "唱腔" in prompt
    # 嚴禁公式化反轉句型
    assert "公式化" in prompt
    # 禁假文青套話與八股詞：禁詞清單單一來源，不另維護一份
    assert "假文青" in prompt
    assert "許多人以為" in FORBIDDEN_DJ_PHRASES
    assert "別以為" in FORBIDDEN_DJ_PHRASES
    for phrase in FORBIDDEN_DJ_PHRASES:
        assert phrase in prompt, phrase
    # 零幻覺
    assert "Google" in prompt
    assert "「無」" in prompt
    # 2026-09-29 真機：不強調時 Gemini 常憑記憶作答 → grounding_chunks 空被 L2 擋
    assert "不准只憑記憶" in prompt
    # 輸出格式
    assert "只輸出台詞" in prompt


def test_build_album_tracklist_prompt():
    """專輯曲目查證 prompt（/tour）：曲目是事實資料，只准 Google 查證、固定編號格式
    （第一行是「1. 」開頭，歌名本身以「無」開頭也不會被 L1 拒答 guard 誤殺）、查不到回「無」。"""
    from dj_prompt_builder import build_album_tracklist_prompt

    prompt = build_album_tracklist_prompt("周杰倫", "范特西")
    assert "周杰倫" in prompt and "范特西" in prompt
    assert "Google" in prompt
    assert "1. " in prompt
    assert "只寫歌名" in prompt
    assert "「無」" in prompt
    # 2026-09-29 真機：《范特西》不強調時 4 次只 1 次附來源（其餘憑記憶→L2 擋），強調後 3/3
    assert "不准只憑記憶" in prompt


def test_material_guard_song_is_lead_material_is_supporting():
    """10/2 使用者定：歌才是主角，素材是綠葉——護欄共用於所有 DJ 口白 prompt。"""
    from dj_prompt_builder import DJ_MATERIAL_GUARD, build_dj_interjection_prompt
    assert "主角" in DJ_MATERIAL_GUARD
    assert "綠葉" in DJ_MATERIAL_GUARD
    assert DJ_MATERIAL_GUARD in build_dj_interjection_prompt("歌曲：周杰倫 - 晴天")


def test_tone_rule_has_no_pun_trigger():
    """LLM 現編中文諧音是能力斷崖（諧音笑話改走 song_jokes 本地查表），風格規則不再誘發。"""
    from dj_prompt_builder import get_dj_unified_rules
    rules = get_dj_unified_rules()
    assert "諧音" not in rules["tone_rule"]
    assert "諧音" not in rules["material_style_rule"]


def test_guide_prompts_do_not_cue_headphones():
    """導聆/歌曲卡第三幕不再引導「戴上耳機」（跟 FALLBACK_GUIDE_TEMPLATE 一致）。"""
    from dj_prompt_builder import build_audiophile_guide_prompt, build_song_card_ingestion_prompt
    assert "耳機" not in build_audiophile_guide_prompt("周杰倫 - 晴天")
    assert "耳機" not in build_song_card_ingestion_prompt("周杰倫 - 晴天")
