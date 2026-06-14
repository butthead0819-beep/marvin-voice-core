"""TDD — 通用使用者提醒節流器（NudgeThrottle）。

任何功能要對某使用者發提醒都走這裡，共用同一套抑制原則：
- 短窗內累積 ≥min_attempts 個有效訊號才觸發（濾掉單次噪音）
- 每 (category, speaker) 每段語音 session 最多觸發一次（防洗版）
- session 邊界由呼叫端在使用者 (重)進語音時呼叫 reset_speaker

呼叫端只在「確有有效訊號」時呼叫 signal()，節流器本身不懂各功能語意。
首個 consumer = 環境噪音喚醒提醒（category="noise"，對應 2026-06-13 showay incident）。
"""
from __future__ import annotations

from nudge_throttle import NudgeThrottle


def test_single_signal_below_threshold_no_fire():
    """窗內僅 1 個訊號 → 未達門檻，不觸發。"""
    t = NudgeThrottle(window_s=60.0, min_attempts=2)
    assert t.signal("noise", "showay", now=1.0) is False


def test_two_signals_in_window_fires():
    """窗內第 2 個訊號 → 達門檻，觸發。"""
    t = NudgeThrottle(window_s=60.0, min_attempts=2)
    assert t.signal("noise", "showay", now=1.0) is False
    assert t.signal("noise", "showay", now=30.0) is True


def test_signals_outside_window_dont_accumulate():
    """兩訊號相隔超過窗口 → 舊的滑出，窗內仍只 1 個 → 不觸發。"""
    t = NudgeThrottle(window_s=60.0, min_attempts=2)
    assert t.signal("noise", "showay", now=1.0) is False
    assert t.signal("noise", "showay", now=100.0) is False


def test_only_fires_once_per_session():
    """觸發過後同 session 再多訊號也不重複（整晚只提醒一次）。"""
    t = NudgeThrottle(window_s=60.0, min_attempts=2)
    t.signal("noise", "showay", now=1.0)
    assert t.signal("noise", "showay", now=2.0) is True
    assert t.signal("noise", "showay", now=3.0) is False
    assert t.signal("noise", "showay", now=4.0) is False


def test_reset_speaker_rearms_all_categories():
    """speaker (重)進語音 → reset 清掉該人所有類別，可再觸發。"""
    t = NudgeThrottle(window_s=60.0, min_attempts=2)
    t.signal("noise", "showay", now=1.0)
    assert t.signal("noise", "showay", now=2.0) is True
    t.reset_speaker("showay")
    assert t.signal("noise", "showay", now=10.0) is False
    assert t.signal("noise", "showay", now=11.0) is True


def test_per_speaker_isolation():
    """A 的訊號不會觸發 B（各自獨立）。"""
    t = NudgeThrottle(window_s=60.0, min_attempts=2)
    assert t.signal("noise", "showay", now=1.0) is False
    assert t.signal("noise", "狗與露", now=2.0) is False
    assert t.signal("noise", "showay", now=3.0) is True


def test_per_category_isolation():
    """同一 speaker 不同類別各自獨立累積與觸發。"""
    t = NudgeThrottle(window_s=60.0, min_attempts=2)
    assert t.signal("noise", "showay", now=1.0) is False
    assert t.signal("low_volume", "showay", now=2.0) is False
    # noise 第 2 次觸發；low_volume 仍只 1 次不觸發
    assert t.signal("noise", "showay", now=3.0) is True
    assert t.signal("low_volume", "showay", now=4.0) is True


def test_reset_speaker_only_affects_that_speaker():
    """reset A 不影響 B 的已觸發狀態。"""
    t = NudgeThrottle(window_s=60.0, min_attempts=2)
    t.signal("noise", "showay", now=1.0)
    t.signal("noise", "showay", now=2.0)  # showay fired
    t.signal("noise", "狗與露", now=3.0)
    t.signal("noise", "狗與露", now=4.0)  # 狗與露 fired
    t.reset_speaker("showay")
    # 狗與露 仍鎖定，showay 已重新武裝
    assert t.signal("noise", "狗與露", now=5.0) is False
    assert t.signal("noise", "showay", now=6.0) is False
    assert t.signal("noise", "showay", now=7.0) is True


def test_per_call_threshold_override():
    """單次呼叫可覆寫門檻（不同功能要求不同靈敏度）。"""
    t = NudgeThrottle(window_s=60.0, min_attempts=2)
    # min_attempts=1 → 首個訊號即觸發
    assert t.signal("urgent", "showay", now=1.0, min_attempts=1) is True
