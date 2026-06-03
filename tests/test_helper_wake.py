"""TDD: 免喚醒詞 task/info 喚醒（helper query）路由決策 pure core。

對應需求：23:32/23:45 那種「沒喊馬文、但講了求助/任務語句」被 IBA task 通道喚醒
的回應，要 (1) 貼文標題不再是「喚醒」(2) 長答案只念短通知、完整內容留貼文。
這裡只測純判斷邏輯；streaming 整合在 voice_controller。
"""
from __future__ import annotations

from helper_wake import (
    HELPER_SPEAK_FULL_MAXLEN,
    helper_speak_plan,
    is_helper_wake,
)


# ── is_helper_wake ────────────────────────────────────────────────────────────

def test_is_helper_wake_true_for_lowvoice_task_or_info():
    """沒喊馬文（voice 低）+ task/info 通道帶起 → helper。"""
    assert is_helper_wake(0.3, "task") is True
    assert is_helper_wake(0.3, "task_search") is True
    assert is_helper_wake(0.3, "info") is True


def test_is_helper_wake_false_when_wakeword_said():
    """喊了馬文 → voice 高（≥0.5）→ 不是 helper（照常整段念，標題不變）。"""
    assert is_helper_wake(1.0, "voice") is False
    assert is_helper_wake(0.6, "task") is False  # 即使 dom=task，喊了名字就不算


def test_is_helper_wake_false_for_non_taskinfo_dom():
    """control / voice 主導 → 非 helper。"""
    assert is_helper_wake(0.3, "control") is False
    assert is_helper_wake(0.3, "voice") is False


def test_is_helper_wake_false_when_voice_score_missing():
    """無 fusion / 舊路徑（voice_score=None）→ 不改原行為。"""
    assert is_helper_wake(None, "task") is False


def test_is_helper_wake_false_when_dom_missing():
    assert is_helper_wake(0.3, None) is False
    assert is_helper_wake(0.3, "") is False


# ── helper_speak_plan ─────────────────────────────────────────────────────────

def test_helper_speak_plan_short_answer_speaks_full():
    mode, text = helper_speak_plan("晚上九點。", "showay")
    assert mode == "full"
    assert text == "晚上九點。"


def test_helper_speak_plan_long_answer_notifies_without_content():
    long = "你說的應該是日本的 Workman。它是專門賣平價工作服與戶外裝備的品牌，品質還算對得起價格。"
    mode, text = helper_speak_plan(long, "showay")
    assert mode == "notify"
    assert "showay" in text          # 點名使用者
    assert "Workman" not in text     # 不念內容，內容留貼文


def test_helper_speak_plan_boundary_at_maxlen():
    """剛好 maxlen → full；多 1 字 → notify。"""
    assert helper_speak_plan("x" * HELPER_SPEAK_FULL_MAXLEN, "a")[0] == "full"
    assert helper_speak_plan("x" * (HELPER_SPEAK_FULL_MAXLEN + 1), "a")[0] == "notify"


def test_helper_speak_plan_strips_whitespace():
    mode, text = helper_speak_plan("  好的  ", "a")
    assert mode == "full"
    assert text == "好的"
