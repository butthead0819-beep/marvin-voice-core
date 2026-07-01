"""TDD — 控制指令拼音 fast-path：糊字/同音字控制指令 → 正規指令，跳過 cleaner LLM。

延伸 MusicFastPath 的拼音 fuzzy 到「控制指令」域（skip/pause/resume/stop）。
STT 常把控制詞糊成同音字（下一首→下一手、切歌→切鴿、暫停→暫聽），精確關鍵字表
會 miss → 掉到 cleaner。拼音 toneless 讓同音字塌成同一字串 → fuzzy 救回。

守門（防閒聊誤觸，對齊 is_short_skip_command 精神）：剝允許前綴（馬文/快…）後，
剩餘須短且整串 fuzz.ratio≥門檻 → 命令要「是」而非「含」；問句/否定/長句被 ratio 稀釋掉。
"""
from __future__ import annotations

import pytest

from command_fastpath import match_command_action, normalize_command


# ── 真陽性：同音字/糊字控制指令 → 正確 action ─────────────────────────────────
@pytest.mark.parametrize("dirty, action", [
    ("下一手", "skip"),      # 手=首 shou
    ("下衣首", "skip"),      # 衣=一 yi
    ("換一手", "skip"),      # 換一首
    ("跳鍋", "skip"),        # 鍋=過 guo（跳過）
    ("切鴿", "skip"),        # 鴿=歌 ge（切歌）
    ("暫聽音樂", "pause"),   # 聽=停 ting（暫停音樂）
    ("繼續撥", "resume"),    # 撥=播 bo（繼續播）
    ("停止撥放", "stop"),    # 撥=播 bo（停止播放）
])
def test_homophone_garble_maps_to_action(dirty, action):
    assert match_command_action(dirty) == action


def test_exact_command_still_matches():
    assert match_command_action("下一首") == "skip"
    assert match_command_action("停止播放") == "stop"


# ── 允許前綴（address/intensifier）剝除後仍命中 ──────────────────────────────
@pytest.mark.parametrize("text", ["馬文下一手", "快換一手", "欸，跳鍋"])
def test_allowed_prefix_stripped_then_matches(text):
    assert match_command_action(text) == "skip"


# ── 真陰性：問句/否定/閒聊/長句 → None（不可誤觸）─────────────────────────────
@pytest.mark.parametrize("text", [
    "為什麼要一直跳過這首歌",   # 問句長句
    "不要下一首",              # 否定：不想跳
    "下雨了好冷",              # 下雨≠下一首，非指令
    "我在聽音樂很開心",         # 提到聽/音樂但非指令
    "你覺得這首歌怎麼樣",       # 閒聊
    "",
    "   ",
])
def test_non_command_returns_none(text):
    assert match_command_action(text) is None


# ── normalize_command：回正規指令字串（供下游 regex agent 比對）──────────────
def test_normalize_returns_canonical_text():
    assert normalize_command("下一手") == "下一首"
    assert normalize_command("繼續撥") == "繼續播"
    assert normalize_command("暫聽音樂") == "暫停音樂"
    assert normalize_command("停止撥放") == "停止播放"


def test_normalize_non_command_returns_none():
    assert normalize_command("你今天過得好嗎") is None
    assert normalize_command("") is None
