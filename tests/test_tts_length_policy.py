"""TTS length policy — 各 task 的字數/時長 gate，截斷時優先在符號處切。

LLM 不聽 prompt 指示時的最後一道防線：DJ 介紹寫「7 秒內」但 LLM 給 30 字 → 截到 7 秒內。
"""
from __future__ import annotations

from tts_length_policy import truncate_for_tts, LIMITS


# 0.3s/中文字（per tts_engine.get_estimated_duration: 0.25 * 1.2）
def _est(text: str) -> float:
    return len(text) * 0.3


# ── policy table 完整性 ───────────────────────────────────────────────────────

def test_known_tasks_have_limits():
    for task in ("music_intro", "callback", "marvin_reply", "scrap"):
        assert task in LIMITS


def test_music_intro_7s():
    """專業 DJ 介紹：歌名 + 歌手 + 年份 + 歌詞重點，預算 7s（~23 字）。"""
    assert LIMITS["music_intro"] == 7.0


def test_callback_7s():
    assert LIMITS["callback"] == 7.0


# ── 不需截斷的情況 ──────────────────────────────────────────────────────────

def test_short_text_unchanged():
    text = "為你放周杰倫的夜曲"  # 9 字 = 2.7s，遠低於 7s
    out, was_cut = truncate_for_tts(text, "music_intro", _est)
    assert out == text
    assert was_cut is False


def test_empty_text_unchanged():
    out, was_cut = truncate_for_tts("", "music_intro", _est)
    assert out == ""
    assert was_cut is False


def test_no_limit_task_unchanged():
    """marvin_reply policy=None → 永不截斷，long 也不動。"""
    long_text = "這是非常非常非常長的回覆，超過二十個字應該也不要切"
    out, was_cut = truncate_for_tts(long_text, "marvin_reply", _est)
    assert out == long_text
    assert was_cut is False


# ── music_intro 截斷（7s budget ≈ 23 中文字）─────────────────────────────────

def test_music_intro_cuts_at_punctuation():
    """超 budget → 在符號處切（找 budget 內最後一個 punctuation）。"""
    # 30 字 = 9s 超 budget。「，」在 index 6, 17。取 budget(23) 內最後一個 = 17。
    text = "周杰倫的夜曲，這是兩千零五年發行的，副歌花葬最動人，一定要聽"
    out, was_cut = truncate_for_tts(text, "music_intro", _est)
    assert was_cut is True
    assert out == "周杰倫的夜曲，這是兩千零五年發行的"


def test_music_intro_cuts_at_chinese_period():
    """budget 內只有「。」→ 切到「。」前。"""
    # 27 字。「。」在 index 11。
    text = "陳奕迅的浮誇是經典歌曲。副歌諷刺娛樂圈名利場太精準了。"
    out, was_cut = truncate_for_tts(text, "music_intro", _est)
    assert was_cut is True
    assert out == "陳奕迅的浮誇是經典歌曲"


def test_music_intro_picks_last_punctuation_in_budget():
    """budget 內有多個符號 → 取最後一個（保留最多內容）。"""
    # 26 字，「，」在 index 4, 10, 18。budget=23 內最後一個 = 18。
    text = "陳奕迅的，浮誇經典歌，副歌諷刺娛樂圈，名利場精準入味"
    out, was_cut = truncate_for_tts(text, "music_intro", _est)
    assert was_cut is True
    assert out == "陳奕迅的，浮誇經典歌，副歌諷刺娛樂圈"


def test_music_intro_no_punctuation_hard_cut_with_ellipsis():
    """無符號 → 硬切到 budget + 「⋯」。"""
    # 29 字無 punct → 硬切 23 字 + ⋯
    text = "周杰倫的夜曲兩千零五年發行的副歌花葬最動人的歌詞充滿戲劇感"
    out, was_cut = truncate_for_tts(text, "music_intro", _est)
    assert was_cut is True
    assert out.endswith("⋯")
    assert len(out) <= 24  # 23 字 + ⋯


# ── callback 截斷（7s budget ≈ 23 中文字）────────────────────────────────────

def test_callback_under_7s_unchanged():
    text = "你之前說過要記得買牛奶，這是 19 字內的提醒哦"  # 含逗號
    # 估算：22 個有效字 ≈ 6.6s
    if _est(text) <= 7.0:
        out, was_cut = truncate_for_tts(text, "callback", _est)
        assert was_cut is False


def test_callback_over_7s_cuts():
    """callback 30+ 字 → 在符號處切到 ~7s 內。"""
    text = "你之前說要記得買牛奶、回家洗衣服、晚上九點開會、別忘了帶充電器"  # 32 字
    out, was_cut = truncate_for_tts(text, "callback", _est)
    assert was_cut is True
    assert _est(out) <= 7.0
    # 切點應該是某個「、」前
    assert "、" not in out[-1:]  # 結尾不該是符號本身


# ── 容忍：符號剛好超 budget 1-2 字 → 仍接受（避免完全硬切）────────────────

def test_punctuation_slightly_over_budget_accepted():
    """budget=23 內無符號，但 budget+1（24）有「，」→ 接受小幅超 budget 換乾淨切。"""
    # 27 字。「，」在 index 24，落在 ceil 容忍區（24..25）。
    text = "為你放這首古典樂貝多芬月光奏鳴曲第三樂章的開頭裡，最美"
    out, was_cut = truncate_for_tts(text, "music_intro", _est)
    assert was_cut is True
    # 切點落在 budget(23) ± ceil(2) 內的符號
    assert len(out) <= 25


# ── unknown task → fail-safe（不截）────────────────────────────────────────

def test_unknown_task_unchanged():
    text = "很長很長的文字" * 5
    out, was_cut = truncate_for_tts(text, "no_such_task", _est)
    assert out == text
    assert was_cut is False
