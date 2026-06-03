"""TDD: 打岔切入時機計算——落子句中段、避開標點。"""
from __future__ import annotations

from manzai_interject import compute_interject_ratio


def _char_at(text, ratio):
    return text[round(ratio * len(text))]


def test_avoids_landing_on_punctuation():
    """0.72 目標剛好落在標點附近 → 微調到非標點字。"""
    text = "週末會下雨。不過水滴落下的軌跡只是在倒數宇宙的終結，一切都沒意義。"
    r = compute_interject_ratio(text, base=0.72)
    pos = round(r * len(text))
    assert text[pos] not in "。，、！？；：,.!?;:…"
    # 仍在 base 附近（沒亂跑）
    assert abs(r - 0.72) < 0.15


def test_keeps_base_when_already_mid_clause():
    """base 點本來就在句中（無標點干擾）→ 大致回 base。"""
    text = "這是一句沒有任何標點符號的長句子用來測試切入點計算邏輯是否正確運作"
    r = compute_interject_ratio(text, base=0.72)
    assert abs(r - 0.72) < 0.05


def test_short_text_returns_base():
    assert compute_interject_ratio("好", base=0.72) == 0.72
    assert compute_interject_ratio("", base=0.72) == 0.72


def test_result_in_unit_range():
    for t in ["a。b。c。d。e。f", "正常的一段話，有逗號，還有句號。結束", "x" * 50]:
        r = compute_interject_ratio(t)
        assert 0.0 <= r <= 1.0


def test_nudged_position_not_adjacent_to_punct():
    """微調後的字元前後 min_gap 內不該有標點。"""
    text = "甲乙丙，丁戊己庚辛壬癸子丑寅卯辰巳午未申，酉戌亥天地玄黃宇宙洪荒"
    r = compute_interject_ratio(text, base=0.72, min_gap=2)
    pos = round(r * len(text))
    puncts = {i for i, c in enumerate(text) if c in "，。"}
    assert all(abs(pos - q) >= 2 for q in puncts)
