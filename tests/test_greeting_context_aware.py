"""進場招呼「讀空氣」：依房間是否熱絡切短/長招呼。

設計背景：Marvin 剛進場聽不到先前的『語音』對話，只能用「人數 + 文字頻道熱度」
當代理判斷房間是否正在熱絡（room_is_active），再決定招呼風格與字數（greeting_plan）。
"""
import pytest

from gemini_router_content import room_is_active, greeting_plan


# ── room_is_active：人數 + 文字熱度 代理 ──────────────────────────────

def test_room_active_when_three_or_more_people():
    # 3 人以上視為熱絡，不管文字熱度
    assert room_is_active(3, "cold") is True
    assert room_is_active(5, None) is True


def test_room_quiet_when_few_people_and_cold():
    assert room_is_active(1, "cold") is False
    assert room_is_active(2, "cold") is False


def test_room_active_when_text_warm_or_hot():
    # 少人但文字頻道有熱度 → 視為熱絡
    assert room_is_active(1, "warm") is True
    assert room_is_active(2, "hot") is True


def test_room_quiet_when_empty_or_unknown():
    assert room_is_active(0, None) is False
    assert room_is_active(None, None) is False


# ── greeting_plan：風格 + 字數預算 ───────────────────────────────────

def test_active_room_gets_brief_style():
    style, budget = greeting_plan(3, active=True)
    assert style == "brief"


def test_quiet_room_gets_ambient_style():
    style, budget = greeting_plan(1, active=False)
    assert style == "ambient"


def test_brief_budget_stays_terse_with_floor():
    # 短招呼：至少 15 字（單人也不會壓到叫不出名字），依人數溫和縮放
    _, b1 = greeting_plan(1, active=True)
    assert b1 == 15
    _, b4 = greeting_plan(4, active=True)
    assert b4 > b1          # 人多時稍長以叫全名字
    assert b4 <= 40         # 但仍維持「簡短報到」量級


def test_ambient_budget_is_fixed_and_longer():
    # 長招呼：固定約 50 字（冷場要多聊、引話題），不隨人數縮放
    _, b1 = greeting_plan(1, active=False)
    _, b5 = greeting_plan(5, active=False)
    assert b1 == 50
    assert b5 == 50


def test_ambient_is_longer_than_brief():
    _, brief = greeting_plan(2, active=True)
    _, ambient = greeting_plan(2, active=False)
    assert ambient > brief
