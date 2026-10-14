"""
TDD — WakeSession 純核心狀態機（wake_session.py）。

全部用數字手動推進 now，不呼叫 time.time()/sleep。
"""
from __future__ import annotations

from wake_session import WakeSession


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
