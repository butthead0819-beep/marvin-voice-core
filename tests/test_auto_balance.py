"""Active Music Balance — target_balance / AutoBalanceState / manual_hold_active /
mixer 慢速 ramp / state proxy / _auto_balance_tick（2026-10-18 使用者定）。"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from auto_balance import (
    AutoBalanceState,
    manual_hold_active,
    target_balance,
)
from local_mixing_source import LocalMixingAudioSource


def _entry(speaker: str, text: str, ts: float) -> dict:
    return {"speaker": speaker, "text": text, "timestamp": ts}


# ---------------------------------------------------------------------------
# target_balance
# ---------------------------------------------------------------------------

def test_target_balance_no_one_online():
    assert target_balance([], 0, now=1000.0) is None


def test_target_balance_quiet_no_recent_human_speech():
    now = 1000.0
    entries = [_entry("A", "今天吃什麼好呢", now - 61.0)]
    assert target_balance(entries, 2, now) == 0.50


def test_target_balance_quiet_only_marvin_speaking():
    now = 1000.0
    entries = [_entry("Marvin", "這首歌不錯喔", now - 5.0)]
    assert target_balance(entries, 2, now) == 0.50


def test_target_balance_some_two_online_one_recent_utterance():
    now = 1000.0
    entries = [_entry("A", "欸剛剛那個笑話", now - 5.0)]
    assert target_balance(entries, 2, now) == 0.70


def test_target_balance_hot_two_online_twenty_utterances():
    now = 1000.0
    entries = [_entry("A" if i % 2 == 0 else "B", f"講話{i}", now - i) for i in range(20)]
    assert target_balance(entries, 2, now) == 0.90


def test_target_balance_solo_ten_utterances_is_hot():
    now = 1000.0
    entries = [_entry("A", f"講話{i}", now - i) for i in range(10)]
    assert target_balance(entries, 1, now) == 0.90


def test_target_balance_solo_nine_utterances_is_some():
    now = 1000.0
    entries = [_entry("A", f"講話{i}", now - i) for i in range(9)]
    assert target_balance(entries, 1, now) == 0.70


# ---------------------------------------------------------------------------
# AutoBalanceState
# ---------------------------------------------------------------------------

def test_state_target_equals_actual_returns_none():
    st = AutoBalanceState()
    assert st.update(0.70, 0.70) is None


def test_state_new_target_needs_two_ticks():
    st = AutoBalanceState()
    assert st.update(0.90, 0.70) is None
    assert st.update(0.90, 0.70) == 0.90


def test_state_target_changes_midway_resets_count():
    st = AutoBalanceState()
    assert st.update(0.90, 0.70) is None
    assert st.update(0.50, 0.70) is None  # 換目標 → 重新算
    assert st.update(0.50, 0.70) == 0.50


def test_state_target_returns_to_actual_midway_clears_count():
    st = AutoBalanceState()
    assert st.update(0.90, 0.70) is None
    assert st.update(0.70, 0.70) is None  # 回到現值 → 清掉 pending
    assert st.update(0.90, 0.70) is None  # 重新從第 1 次算
    assert st.update(0.90, 0.70) == 0.90


# ---------------------------------------------------------------------------
# manual_hold_active
# ---------------------------------------------------------------------------

def test_manual_hold_active_within_599s():
    assert manual_hold_active(1000.0, 1599.0) is True


def test_manual_hold_active_at_600s_false():
    assert manual_hold_active(1000.0, 1600.0) is False


def test_manual_hold_active_never_manually_set():
    assert manual_hold_active(float('-inf'), 1000.0) is False


# ---------------------------------------------------------------------------
# mixer 慢速 ramp
# ---------------------------------------------------------------------------

def test_mixer_set_balance_ramp_s_sets_slow_step():
    mixer = LocalMixingAudioSource(balance=0.50)
    mixer._balance_cur = 0.50
    mixer.set_balance(0.90, ramp_s=3.0)
    assert mixer._balance_step_active == pytest.approx(0.4 / 150)


def test_mixer_set_balance_default_ramp_is_button_speed():
    mixer = LocalMixingAudioSource(balance=0.50)
    mixer._balance_cur = 0.50
    mixer.set_balance(0.90, ramp_s=3.0)  # 先變慢
    mixer.set_balance(0.70)              # 不帶 ramp_s → 恢復按鈕速度
    assert mixer._balance_step_active == 0.02


# ---------------------------------------------------------------------------
# state proxy
# ---------------------------------------------------------------------------

def _new_controller():
    from cogs.voice_controller import VoiceController
    vc = VoiceController.__new__(VoiceController)
    vc.bot = MagicMock()
    vc.bot.cogs.get.return_value = None
    vc._tts_balance_local = 0.50
    vc._mixer = None
    return vc


def test_manual_setter_updates_manual_at():
    vc = _new_controller()
    vc._balance_manual_at = float('-inf')
    vc.tts_balance = 0.70
    assert vc._balance_manual_at != float('-inf')


def test_apply_auto_balance_does_not_update_manual_at_and_ramps_mixer():
    vc = _new_controller()
    vc._balance_manual_at = float('-inf')
    vc._mixer = MagicMock()
    vc._apply_auto_balance(0.9)
    assert vc._balance_manual_at == float('-inf')
    vc._mixer.set_balance.assert_called_once_with(0.9, ramp_s=3.0)


# ---------------------------------------------------------------------------
# _auto_balance_tick
# ---------------------------------------------------------------------------

def _fake_social_self(entries, n_online, manual_at=float('-inf'), tts_balance=0.50):
    from auto_balance import AutoBalanceState

    class _Fake:
        pass

    s = _Fake()
    s.bot = MagicMock()
    s.bot.engine.conv_buffer.get_history.return_value = entries
    s.get_online_members = lambda: ["a"] * n_online
    s.tts_balance = tts_balance
    s._auto_balance_state = AutoBalanceState()
    s._balance_manual_at = manual_at
    s._apply_auto_balance = MagicMock()
    return s


def test_auto_balance_tick_applies_after_two_hot_ticks():
    from cogs.voice_controller_social import ProactiveSocialMixin

    now = 1000.0
    entries = [_entry("A" if i % 2 == 0 else "B", f"講話{i}", now - i) for i in range(20)]
    s = _fake_social_self(entries, n_online=2)
    import time as time_mod
    orig_time = time_mod.time
    try:
        time_mod.time = lambda: now
        ProactiveSocialMixin._auto_balance_tick(s)
        s._apply_auto_balance.assert_not_called()
        ProactiveSocialMixin._auto_balance_tick(s)
        s._apply_auto_balance.assert_called_once_with(0.90)
    finally:
        time_mod.time = orig_time


def test_auto_balance_tick_skipped_during_manual_hold(monkeypatch):
    from cogs.voice_controller_social import ProactiveSocialMixin
    import time as time_mod

    now_mono = time_mod.monotonic()
    entries = [_entry("A", "講話", time_mod.time())]
    s = _fake_social_self(entries, n_online=2, manual_at=now_mono)
    ProactiveSocialMixin._auto_balance_tick(s)
    ProactiveSocialMixin._auto_balance_tick(s)
    s._apply_auto_balance.assert_not_called()


def test_auto_balance_tick_noop_when_kill_switch_off(monkeypatch):
    from cogs.voice_controller_social import ProactiveSocialMixin

    import time as time_mod

    monkeypatch.setenv("MARVIN_AUTO_BALANCE", "0")
    now = time_mod.time()  # 用真實時間：沒有 kill switch 時這組 entries 一定會套用 0.90
    entries = [_entry("A" if i % 2 == 0 else "B", f"講話{i}", now - i) for i in range(20)]
    s = _fake_social_self(entries, n_online=2)
    ProactiveSocialMixin._auto_balance_tick(s)
    ProactiveSocialMixin._auto_balance_tick(s)
    s._apply_auto_balance.assert_not_called()


def test_auto_balance_tick_kill_switch_on_by_default(monkeypatch):
    """對照組：同一組 entries 沒設 kill switch 會套用（證明上一個測試不是空轉）。"""
    from cogs.voice_controller_social import ProactiveSocialMixin
    import time as time_mod

    monkeypatch.delenv("MARVIN_AUTO_BALANCE", raising=False)
    now = time_mod.time()
    entries = [_entry("A" if i % 2 == 0 else "B", f"講話{i}", now - i) for i in range(20)]
    s = _fake_social_self(entries, n_online=2)
    ProactiveSocialMixin._auto_balance_tick(s)
    ProactiveSocialMixin._auto_balance_tick(s)
    s._apply_auto_balance.assert_called_once_with(0.90)
