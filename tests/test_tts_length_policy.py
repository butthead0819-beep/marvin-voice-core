"""TTS length policy — 各 task 的字數/時長 gate，截斷時優先在符號處切。

LLM 不聽 prompt 指示時的最後一道防線：DJ 介紹寫「15 秒內」但 LLM 給 60 字 → 截到 15 秒內。
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


def test_dj_story_gate_matches_60_char_budget():
    """2026-07-17 使用者：雞湯文改成 10 秒。

    ⚠️ gate 是「估算器秒數」不是真實秒數：估算器 0.3s/中文字（保守），真實
    edge-tts ≈0.17s/字。真實 10s ≈ 57-60 字 → 估算器口徑 60×0.3 = 18s。
    照字面把 gate 設 10.0 會在 33 字就砍斷（真實才 5.7s）＝「狗與露」殘句重演。
    """
    assert LIMITS["dj_story"] == 18.0
    assert _est("字" * 60) <= LIMITS["dj_story"], "60 字（真實≈10s）不該被 gate 砍"
    assert _est("字" * 70) > LIMITS["dj_story"], "70 字（超出預算）應被 gate 攔下"


def test_music_intro_5s():
    """DJ 播報上限 5s（~16 字）：2026-07-13 從 15s 收到 5s（DJ 話太多）。"""
    assert LIMITS["music_intro"] == 5.0


def test_callback_15s():
    assert LIMITS["callback"] == 15.0


# ── 不需截斷的情況 ──────────────────────────────────────────────────────────

def test_short_text_unchanged():
    text = "為你放周杰倫的夜曲"  # 9 字 = 2.7s，遠低於 15s
    out, was_cut = truncate_for_tts(text, "music_intro", _est)
    assert out == text
    assert was_cut is False


def test_empty_text_unchanged():
    out, was_cut = truncate_for_tts("", "music_intro", _est)
    assert out == ""
    assert was_cut is False


def test_no_limit_task_unchanged():
    """marvin_reply policy=None → 永不截斷，long 也不動。"""
    long_text = "這是非常非常非常長的回覆，超過五十個字應該也不要切，這是測試用的字串應該夠長"
    out, was_cut = truncate_for_tts(long_text, "marvin_reply", _est)
    assert out == long_text
    assert was_cut is False


# ── music_intro 截斷（5s budget ≈ 16 中文字）─────────────────────────────────

def test_music_intro_cuts_at_punctuation():
    """超 budget → 在符號處切（找 budget 內最後一個 punctuation）。"""
    # 24 字 = 7.2s 超 budget(5s=16字)。「，」在 index 6, 14, 20。budget(16) 內最後一個 = 14。
    text = "周杰倫的夜曲，兩千零五年的歌，副歌很動人，值得聽"
    out, was_cut = truncate_for_tts(text, "music_intro", _est)
    assert was_cut is True
    # 切到 budget 內最後一個「，」前
    assert out == "周杰倫的夜曲，兩千零五年的歌"


def test_music_intro_cuts_at_chinese_period():
    """budget 內只有「。」→ 切到「。」前。"""
    text = "陳奕迅的浮誇是兩千零五年發行的香港歌壇經典作品深刻地諷刺了娛樂圈名利場的虛偽。副歌的情緒爆發堪稱整個職涯的巔峰之作"
    assert len(text) > 50, f"text 必須 > 50 字才觸發截斷，實際 {len(text)}"
    out, was_cut = truncate_for_tts(text, "music_intro", _est)
    assert was_cut is True
    assert "。" not in out[-1:]


def test_music_intro_picks_last_punctuation_in_budget():
    """budget 內有多個符號 → 取最後一個（保留最多內容）。"""
    text = "陳奕迅的浮誇，是經典歌曲，副歌深刻諷刺娛樂圈名利場的虛偽，這首歌絕對是當代華語樂壇代表作，值得反覆聽到老"
    assert len(text) > 50, f"len={len(text)}"
    out, was_cut = truncate_for_tts(text, "music_intro", _est)
    assert was_cut is True
    assert len(out) <= 50  # 切點該在 budget 內
    assert "，" not in out[-1:]


def test_music_intro_no_punctuation_hard_cut_with_ellipsis():
    """無符號 → 硬切到 budget + 「⋯」。"""
    text = "周杰倫的夜曲兩千零五年發行的副歌花葬最動人的歌詞充滿戲劇感是當代華語流行樂壇代表作品經典中的經典絕對值得一聽"
    assert len(text) > 50, f"len={len(text)}"
    out, was_cut = truncate_for_tts(text, "music_intro", _est)
    assert was_cut is True
    assert out.endswith("⋯")
    assert len(out) <= 51  # 50 字 + ⋯


# ── callback 截斷（15s budget ≈ 50 中文字）───────────────────────────────────

def test_callback_under_15s_unchanged():
    text = "你之前說過要記得買牛奶，這是 30 字內的提醒哦不要忘了喔"  # 含逗號，估算 < 15s
    if _est(text) <= 15.0:
        out, was_cut = truncate_for_tts(text, "callback", _est)
        assert was_cut is False


def test_callback_over_15s_cuts():
    """callback 60+ 字 → 在符號處切到 ~15s 內。"""
    text = "你之前說要記得買牛奶、回家洗衣服、晚上九點開會、別忘了帶充電器、出門前要餵狗、記得鎖門、把垃圾拿出去倒、洗碗"
    assert len(text) > 50, f"len={len(text)}"
    out, was_cut = truncate_for_tts(text, "callback", _est)
    assert was_cut is True
    assert _est(out) <= 15.0
    assert "、" not in out[-1:]


# ── 容忍：符號剛好超 budget 1-2 字 → 仍接受（避免完全硬切）────────────────

def test_punctuation_slightly_over_budget_accepted():
    """budget=50 內無符號，但 budget+1/2（51-52）有「，」→ 接受小幅超 budget 換乾淨切。"""
    # 構造：前 51 字無 punct，index 51 是「，」，後接其他字 → 仍要 > 50 字才觸發
    text = "為你放這首古典樂貝多芬月光奏鳴曲第三樂章的開頭最美的旋律啊我超喜歡這首歌耶，這首是世紀經典之作真的好聽"
    assert len(text) > 50, f"len={len(text)}"
    out, was_cut = truncate_for_tts(text, "music_intro", _est)
    assert was_cut is True
    # 切點落在 budget(50) ± ceil(2) 內 → len 不超 52
    assert len(out) <= 52


# ── unknown task → fail-safe（不截）────────────────────────────────────────

def test_unknown_task_unchanged():
    text = "很長很長的文字" * 10
    out, was_cut = truncate_for_tts(text, "no_such_task", _est)
    assert out == text
    assert was_cut is False
