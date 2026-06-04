"""招呼字數依在場人數縮放（2026-06-04）。

原本 greeting 固定「60 字內」，人多時每人被壓到 <10 字、叫不全名字。改成
每位約 13 字（範圍 10-15）的動態預算，隨人數增加。
"""
from __future__ import annotations

from gemini_router_content import greeting_char_budget


def test_budget_scales_with_player_count():
    assert greeting_char_budget(1) == 13
    assert greeting_char_budget(4) == 52
    assert greeting_char_budget(6) == 78
    assert greeting_char_budget(5) > greeting_char_budget(4)   # 嚴格遞增


def test_budget_per_player_in_10_to_15_range():
    for n in range(1, 9):
        per = greeting_char_budget(n) / n
        assert 10 <= per <= 15, f"n={n} per={per} 超出 10-15"


def test_budget_floors_at_one_player_for_zero_or_none():
    assert greeting_char_budget(0) >= 10      # 空房也不回 0
    assert greeting_char_budget(None) >= 10   # None 視為 1 人份
