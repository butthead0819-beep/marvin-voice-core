"""
TDD — WakeSession 純核心狀態機（wake_session.py）。

全部用數字手動推進 now，不呼叫 time.time()/sleep。
"""
from __future__ import annotations

import pytest

from wake_session import WakeSession, is_closing_phrase


def test_on_utterance_false_before_replied():
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    assert s.on_utterance("A", 1.0, marvin_speaking=False) is False


def test_on_utterance_true_for_target_false_for_others_after_replied():
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    s.on_replied(0.5)
    assert s.on_utterance("A", 1.0, marvin_speaking=False) is True
    s2 = WakeSession()
    s2.on_wake_accepted("A", 0.0)
    s2.on_replied(0.5)
    assert s2.on_utterance("B", 1.0, marvin_speaking=False) is False


def test_marvin_speaking_blocks_but_updates_last_speech():
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    s.on_replied(0.5)   # last_speech = 0.5
    assert s.on_utterance("A", 10.0, marvin_speaking=True) is False
    assert s.last_speech == 10.0
    # idle 應以新 last_speech (10.0) 起算：10.0+119=129 仍在窗內
    assert s.on_utterance("A", 129.0, marvin_speaking=False) is True


def test_utterance_within_await_window_rejected_then_accepted_after_replied():
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    s.on_replied(0.5)
    assert s.on_utterance("A", 1.0, marvin_speaking=False) is True   # 接住，awaiting_since=1.0
    assert s.on_utterance("A", 20.0, marvin_speaking=False) is False  # 30s 內，上一句還在處理
    assert s.on_replied(25.0) is True
    assert s.on_utterance("A", 26.0, marvin_speaking=False) is True


def test_idle_timeout_ends_session():
    s = WakeSession()
    s.on_wake_accepted("A", 80.0)   # arm 在 on_replied 前 30s 內，避免 stale_arm
    s.on_replied(100.0)   # last_speech = 100.0
    assert s.on_utterance("A", 221.0, marvin_speaking=False) is False
    assert s.target is None

    s2 = WakeSession()
    s2.on_wake_accepted("A", 80.0)
    s2.on_replied(100.0)
    assert s2.on_utterance("A", 219.0, marvin_speaking=False) is True


def test_three_consecutive_ignored_ends_session():
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    s.on_replied(0.0)
    for t in (1.0, 2.0, 3.0):
        assert s.on_utterance("A", t, marvin_speaking=False) is True
        s.on_ignored("A", t + 0.1)
    assert s.target is None


def test_unanswered_timeout_counts_and_ends_on_third():
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    s.on_replied(0.0)
    now = 1.0
    assert s.on_utterance("A", now, marvin_speaking=False) is True   # awaiting_since=1.0
    now = 1.0 + 31.0
    assert s.on_utterance("A", now, marvin_speaking=False) is True   # 逾時計次 unanswered=1，又接住
    assert s.unanswered == 1
    now = now + 31.0
    assert s.on_utterance("A", now, marvin_speaking=False) is True   # unanswered=2
    assert s.unanswered == 2
    now = now + 31.0
    assert s.on_utterance("A", now, marvin_speaking=False) is False  # 第三次逾時 → 結束
    assert s.target is None


def test_on_wake_accepted_transfers_to_new_target():
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    s.on_replied(0.5)
    s.on_wake_accepted("B", 10.0)
    assert s.on_utterance("A", 11.0, marvin_speaking=False) is False
    assert s.on_utterance("B", 11.0, marvin_speaking=False) is False  # 還沒 on_replied
    s.on_replied(10.5)
    assert s.on_utterance("B", 11.0, marvin_speaking=False) is True


def test_on_wake_accepted_same_target_does_not_reset():
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    s.on_replied(0.0)
    s.on_utterance("A", 1.0, marvin_speaking=False)   # awaiting_since=1.0
    s.on_ignored("A", 2.0)   # unanswered=1
    assert s.unanswered == 1
    opened_before = s.opened
    s.on_wake_accepted("A", 3.0)   # 本人在 session 中，不重置
    assert s.unanswered == 1
    assert s.opened == opened_before


def test_on_replied_returns_true_only_on_transition():
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    assert s.on_replied(0.5) is True    # 剛 arm → True
    assert s.on_replied(1.0) is False   # 已聆聽中，無待處理句 → False
    s.on_utterance("A", 2.0, marvin_speaking=False)   # 接住一句
    assert s.on_replied(3.0) is True    # 轉換 → True


def test_stale_arm_ends_session():
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    assert s.on_replied(31.0) is False
    assert s.target is None


def test_on_ignored_before_opened_ends_session():
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    s.on_ignored("A", 1.0)
    assert s.target is None


def test_on_replied_false_without_target():
    s = WakeSession()
    assert s.on_replied(1.0) is False


def test_on_replied_resets_unanswered_count():
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    s.on_replied(0.0)
    s.on_utterance("A", 1.0, marvin_speaking=False)
    s.on_ignored("A", 2.0)
    s.on_utterance("A", 3.0, marvin_speaking=False)
    s.on_ignored("A", 4.0)
    assert s.unanswered == 2
    s.on_replied(5.0)
    assert s.unanswered == 0
    s.on_utterance("A", 6.0, marvin_speaking=False)
    s.on_ignored("A", 7.0)
    s.on_utterance("A", 8.0, marvin_speaking=False)
    s.on_ignored("A", 9.0)
    assert s.unanswered == 2
    assert s.target == "A"   # 還沒到第三次，session 仍在


def test_on_utterance_false_when_not_opened_even_after_await_window():
    """arm 後一直沒回應也沒被 gate 拒（靜默丟棄）：過了 await 窗仍不准免喚醒接話。"""
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    assert s.on_utterance("A", 31.0, marvin_speaking=False) is False


# ── v2：query_mode / closing phrase / music action ───────────────────────────

@pytest.mark.parametrize("text", [
    "謝謝", "謝謝馬文", "知道了", "好了啦", "OK", "馬文 謝謝你", "了解！",
    "沒事了謝謝", "好了謝啦", "OK 謝謝馬文",
    "嗯奧克嗯 OK", "不用了謝謝", "先這樣", "嗯，好了", "停", "結束了", "okay", "沒事",
])
def test_is_closing_phrase_true(text):
    assert is_closing_phrase(text) is True


@pytest.mark.parametrize("text", [
    "謝謝你上次推薦的歌那首叫什麼", "好了沒", "知道了嗎他幾歲", "",
    "沒事了謝謝你上次推薦的歌", "好了上見",
    "不用等一下啊，那天下禮拜去幹沒有那麼急", "停車場在哪", "OK 那你幫我查一下",
    "可以了嗎", "嗯",
])
def test_is_closing_phrase_false(text):
    assert is_closing_phrase(text) is False


def test_on_utterance_closing_phrase_ends_session():
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    s.on_replied(0.5)
    assert s.on_utterance("A", 1.0, marvin_speaking=False, text="謝謝") is False
    assert s.target is None


def test_on_utterance_non_closing_text_still_caught():
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    s.on_replied(0.5)
    assert s.on_utterance("A", 1.0, marvin_speaking=False, text="他上一張專輯呢") is True
    assert s.target == "A"


def test_on_music_action_ends_session():
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    s.on_replied(0.5)
    s.on_music_action(1.0)
    assert s.target is None


def test_on_music_action_noop_without_target():
    s = WakeSession()
    s.on_music_action(1.0)   # 不炸
    assert s.target is None


def test_note_grounded_sets_query_mode_after_opened():
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    s.on_replied(0.5)
    s.note_grounded("A")
    assert s.wants_grounded_followup("A") is True
    assert s.wants_grounded_followup("B") is False


def test_note_grounded_before_opened_not_sticky():
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)   # 只 arm，未 on_replied
    s.note_grounded("A")
    assert s.wants_grounded_followup("A") is False


def test_note_grounded_for_non_target_ignored():
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    s.on_replied(0.5)
    s.note_grounded("B")
    assert s.wants_grounded_followup("A") is False


def test_transfer_to_new_target_resets_query_mode():
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    s.on_replied(0.5)
    s.note_grounded("A")
    assert s.wants_grounded_followup("A") is True
    s.on_wake_accepted("B", 10.0)
    assert s.wants_grounded_followup("B") is False
    s.on_replied(10.5)
    assert s.wants_grounded_followup("B") is False


# ── B：追問黏著連續查不到 → 結束 session（2026-10-15 事故）─────────────────

def test_on_followup_result_two_misses_still_target_third_ends():
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    s.on_replied(0.5)
    s.on_followup_result("A", False)
    assert s.target == "A"
    s.on_followup_result("A", False)
    assert s.target == "A"
    s.on_followup_result("A", False)
    assert s.target is None


def test_on_followup_result_hit_resets_miss_count():
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    s.on_replied(0.5)
    s.on_followup_result("A", False)
    s.on_followup_result("A", False)
    s.on_followup_result("A", True)
    assert s.target == "A"
    s.on_followup_result("A", False)
    s.on_followup_result("A", False)
    assert s.target == "A"


def test_on_followup_result_non_target_speaker_not_counted():
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    s.on_replied(0.5)
    s.on_followup_result("B", False)
    s.on_followup_result("B", False)
    s.on_followup_result("B", False)
    assert s.target == "A"
    assert s.followup_misses == 0


def test_followup_misses_survives_on_replied_but_not_fixed_by_it():
    """事故序列：每輪 on_utterance → on_followup_result(False) → on_replied，
    on_replied 不會救回（followup_misses 獨立於 unanswered），第 3 輪後結束。"""
    s = WakeSession()
    s.on_wake_accepted("A", 0.0)
    s.on_replied(0.0)
    now = 1.0
    for i in range(3):
        assert s.on_utterance("A", now, marvin_speaking=False) is True
        s.on_followup_result("A", False)
        if i < 2:
            assert s.target == "A"
            s.on_replied(now + 0.5)
        now += 1.0
    assert s.target is None
