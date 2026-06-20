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


def _call(vc, is_fast, *, fusion=None):
    if fusion is None:
        fusion = MagicMock()
        fusion.is_open.return_value = False
    return vc._apply_wake_guards("陳進文", "馬文你好", 100.0, None, is_fast,
                                 fusion, "voice", 0.9, 0.8)


# ── 乾淨接受：開 Response Lock + 記 segment ──────────────────────────────────
def test_clean_accept_opens_response_lock_and_records():
    vc = _make_vc()
    is_fast, is_echo = _call(vc, True)
    assert is_fast is True and is_echo is False
    assert vc._wake_response_pending is True
    assert "陳進文_100.0" in vc.processed_wake_segments


# ── Response Lock：回應進行中壓抑快速喚醒 ────────────────────────────────────
def test_response_lock_suppresses():
    vc = _make_vc(_wake_response_pending=True, _wake_accepted_time=time.time())
    is_fast, is_echo = _call(vc, True)
    assert is_fast is False


# ── Echo Guard：TTS 播放中抑制（且 _current_tts_text 非空＝不放行 bypass）────
def test_echo_guard_suppresses_during_tts_playback():
    vc = _make_vc(_is_playing_audio=True, _current_tts_text="馬文正在說話")
    is_fast, is_echo = _call(vc, True)
    assert is_fast is False
    assert is_echo is True


# ── Global Wake Guard：2s 內已喚醒 → 壓抑 ───────────────────────────────────
def test_global_wake_guard_suppresses_within_2s():
    vc = _make_vc(_last_global_wake_time=time.time())
    is_fast, is_echo = _call(vc, True)
    assert is_fast is False


# ── Follow-up override：非 fast 但 fusion 視窗開 → 拉回 fast ─────────────────
def test_followup_window_overrides_to_fast():
    vc = _make_vc()
    fusion = MagicMock()
    fusion.is_open.return_value = True
    is_fast, is_echo = _call(vc, False, fusion=fusion)
    assert is_fast is True
    assert is_echo is False


# ── Storm Guard：風暴中壓抑 ──────────────────────────────────────────────────
def test_storm_guard_suppresses_during_storm():
    vc = _make_vc(_storm_active=True, _storm_last_wake_time=time.time())
    is_fast, is_echo = _call(vc, True)
    assert is_fast is False
