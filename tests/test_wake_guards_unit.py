"""
_apply_wake_guards 單元測試 —— 把 handle_stt_result 中段的喚醒守衛叢集
（Double Wake / Response Lock / Storm / Echo + Strong-Voice Bypass / Global /
Follow-up override）抽成方法後固定其契約。

契約：吃 is_fast（+ wake context），跑完整守衛鏈後回 (is_fast, is_echo)；
is_duplicate/now/segment_id 純內部；接受喚醒時記錄 segment + 開 Response Lock +
風暴計數。這段是回授防護的安全核心，行為不變的整體保證另有既有 wake 整合測試。
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_vc(**over):
    from cogs.voice_controller import VoiceController
    vc = VoiceController.__new__(VoiceController)
    vc.processed_wake_segments = {}
    vc.last_wake_time = {}
    vc._wake_response_pending = False
    vc._wake_accepted_time = 0.0
    vc._storm_active = False
    vc._storm_last_wake_time = 0.0
    vc._wake_burst_times = []
    vc._last_global_wake_time = 0.0
    vc._is_playing_audio = False       # is_playing_audio property 的 backing field
    vc._tts_echo_cooldown_until = 0.0
    vc._current_tts_text = ""
    vc.game_mode = False
    vc._nudges = MagicMock()
    vc._nudges.signal.return_value = False
    vc._send_noise_nudge = AsyncMock()
    for k, v in over.items():
        setattr(vc, k, v)
    return vc


def _call(vc, is_fast, *, fusion=None, is_text_input=False):
    if fusion is None:
        fusion = MagicMock()
        fusion.is_open.return_value = False
    return vc._apply_wake_guards("陳進文", "馬文你好", 100.0, None, is_fast,
                                 fusion, "voice", 0.9, 0.8,
                                 is_text_input=is_text_input)


# ── 乾淨接受：開 Response Lock + 記 segment ──────────────────────────────────
def test_clean_accept_opens_response_lock_and_records():
    vc = _make_vc()
    is_fast, is_echo, reason = _call(vc, True)
    assert is_fast is True and is_echo is False
    assert vc._wake_response_pending is True
    assert "陳進文_100.0" in vc.processed_wake_segments
    assert reason == "wake_word"


# ── Response Lock：回應進行中壓抑快速喚醒 ────────────────────────────────────
def test_response_lock_suppresses():
    vc = _make_vc(_wake_response_pending=True, _wake_accepted_time=time.time())
    is_fast, is_echo, reason = _call(vc, True)
    assert is_fast is False
    assert reason == "guard_response_lock"


# ── Echo Guard：TTS 播放中抑制（且 _current_tts_text 非空＝不放行 bypass）────
def test_echo_guard_suppresses_during_tts_playback():
    vc = _make_vc(_is_playing_audio=True, _current_tts_text="馬文正在說話")
    is_fast, is_echo, reason = _call(vc, True)
    assert is_fast is False
    assert is_echo is True
    assert reason == "guard_echo"


# ── Global Wake Guard：2s 內已喚醒 → 壓抑 ───────────────────────────────────
def test_global_wake_guard_suppresses_within_2s():
    vc = _make_vc(_last_global_wake_time=time.time())
    is_fast, is_echo, reason = _call(vc, True)
    assert is_fast is False
    assert reason == "guard_global"


# ── Follow-up override：非 fast 但 fusion 視窗開 → 拉回 fast ─────────────────
def test_followup_window_overrides_to_fast():
    vc = _make_vc()
    fusion = MagicMock()
    fusion.is_open.return_value = True
    is_fast, is_echo, reason = _call(vc, False, fusion=fusion)
    assert is_fast is True
    assert is_echo is False
    assert reason == "window_question"


# ── Storm Guard：風暴中壓抑 ──────────────────────────────────────────────────
def test_storm_guard_suppresses_during_storm():
    vc = _make_vc(_storm_active=True, _storm_last_wake_time=time.time())
    is_fast, is_echo, reason = _call(vc, True)
    assert is_fast is False
    assert reason == "guard_storm"


# ── reason 表（意圖漏斗步驟 1）──────────────────────────────────────────────

def test_reason_text_input():
    vc = _make_vc()
    is_fast, is_echo, reason = _call(vc, True, is_text_input=True)
    assert reason == "text"


def test_reason_guard_per_speaker():
    vc = _make_vc(last_wake_time={"陳進文": time.time()})
    is_fast, is_echo, reason = _call(vc, True)
    assert is_fast is False
    assert reason == "guard_per_speaker"


def test_reason_guard_double_wake():
    vc = _make_vc()
    vc.processed_wake_segments["陳進文_100.0"] = "A"
    is_fast, is_echo, reason = _call(vc, True)
    assert is_fast is False
    assert reason == "guard_double_wake"


def test_reason_bypass_strong_voice():
    vc = _make_vc(_is_playing_audio=True, _current_tts_text="")
    vc._strong_voice_bypass_echo = MagicMock(return_value=True)
    is_fast, is_echo, reason = _call(vc, True)
    assert is_fast is True
    assert is_echo is False
    assert reason == "bypass_strong_voice"


def test_reason_bypass_intent():
    vc = _make_vc(_is_playing_audio=True, _current_tts_text="")
    vc._strong_voice_bypass_echo = MagicMock(return_value=False)
    vc._intent_bypass_echo = MagicMock(return_value=True)
    is_fast, is_echo, reason = _call(vc, True)
    assert is_fast is True
    assert is_echo is False
    assert reason == "bypass_intent"


def test_reason_window_session():
    from wake_session import WakeSession
    session = WakeSession()
    session.on_wake_accepted("陳進文", time.time())
    session.on_replied(time.time())
    vc = _make_vc(_wake_session=session, _mixer=None)
    is_fast, is_echo, reason = _call(vc, False)
    assert is_fast is True
    assert reason == "window_session"


def test_reason_non_wake_is_none():
    vc = _make_vc()
    fusion = MagicMock()
    fusion.is_open.return_value = False
    is_fast, is_echo, reason = _call(vc, False, fusion=fusion)
    assert is_fast is False
    assert reason is None


def test_followup_override_kind_not_called_when_is_fast():
    """懶求值：is_fast=True 時絕不能呼叫 _followup_override_kind（它有副作用）。"""
    vc = _make_vc()
    vc._followup_override_kind = MagicMock(side_effect=AssertionError("不該被呼叫"))
    is_fast, is_echo, reason = _call(vc, True)
    vc._followup_override_kind.assert_not_called()
