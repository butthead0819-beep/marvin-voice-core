"""TDD：DJ 扭蛋式動機組裝器（dj_gacha_narrator）。

驗證：
1. pick_gacha_motivation：依據可用素材（song_card: audiophile_guide, lyric_hook）
   與當前場景（topic, conv_lines），扭蛋抽出 3 種世俗說話動機之一：
   - irony: 抓矛盾吐槽（Spot the Irony）
   - tea: 爆世俗小八卦（Spill the Tea）
   - hook: 丟聽覺懸念（Drop the Hook）
   （vibe/社群熱評標籤已拔除——LLM 講的社群評論多半是幻覺）
2. 禁 AI 塑膠感：不使用假文青、角色扮演濾鏡，維持 Marvin 唯一老友本色。
3. 降級安全：無歌曲卡時安全回退 None，不干擾既有 pipeline。
"""
from __future__ import annotations

import pytest

from dj_gacha_narrator import GachaMotivation, pick_gacha_motivation


def test_pick_gacha_motivation_with_lyric_hook():
    song_card = {
        "audiophile_guide": "前奏木吉他刷弦一出來，就是千禧年代的校園回憶。",
        "lyric_hook": {"quote": "從前從前有個人愛妳很久", "subtext": "青春無疾而終的遺憾"},
    }
    # 強制指定動機為 irony
    cue = pick_gacha_motivation(song_card, topic="剛才大家在聊加班", forced_mode="irony")
    assert cue is not None
    assert cue.mode == "irony"
    assert "從前從前有個人愛妳很久" in cue.instruction
    assert "吐槽" in cue.instruction or "反差" in cue.instruction


def test_pick_gacha_motivation_forced_vibe_returns_none():
    """vibe 動機已隨社群熱評標籤拔除——即使 song_card 帶著舊格式 social_lore 欄位，
    forced_mode="vibe" 一律回 None（LLM 講的社群評論多半是幻覺，見 2026-09-30 使用者定）。"""
    song_card = {
        "audiophile_guide": "前奏木吉他刷弦一出來，就是千禧年代的校園回憶。",
        "social_lore": {"tag": "全台KTV必點但唱不上去的神曲", "context": "副歌高音直接破音"},
        "lyric_hook": None,
    }
    assert pick_gacha_motivation(song_card, forced_mode="vibe") is None


def test_pick_gacha_motivation_with_audiophile_hook():
    song_card = {
        "audiophile_guide": "注意左聲道那把二胡，在金屬節奏裡突然離調出現。",
        "lyric_hook": None,
    }
    cue = pick_gacha_motivation(song_card, forced_mode="hook")
    assert cue is not None
    assert cue.mode == "hook"
    assert "聽覺" in cue.instruction or "耳朵" in cue.instruction


def test_pick_gacha_motivation_fallback_when_empty():
    assert pick_gacha_motivation(None) is None
    assert pick_gacha_motivation({}) is None
    assert pick_gacha_motivation({"audiophile_guide": ""}) is None


def test_pick_gacha_motivation_random_selection():
    song_card = {
        "audiophile_guide": "二胡與重金屬吉他交織，注意聲場定位。",
        "lyric_hook": {"quote": "快使用雙截棍", "subtext": "含糊咬字"},
    }
    # 不指定 forced_mode，應隨機選出合法的 GachaMotivation
    cue = pick_gacha_motivation(song_card)
    assert cue is not None
    assert cue.mode in {"irony", "tea", "hook"}
    assert len(cue.instruction) > 10
