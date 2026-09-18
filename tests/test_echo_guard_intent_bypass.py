"""TDD — Echo Guard 意圖 bypass（放歌中喚醒詞被 STT 聽糊，但意圖明確的指令要放行）。

2026-09-18：狗與露放歌時說「麻煩播放我不想下單」（應為「馬文播放…」STT 聽糊）、
「幫我下一首」、「把我們播放迪拜人」，IBA 都判 wake=True（total 0.557/0.569/0.557，
dom=task/control/task），但因為 v=0.3 不符合 Strong-Voice Bypass 的門檻，被 Echo
Guard 全擋。同時段兩句閒聊 total=0.346 dom=task 也被擋——那兩句要繼續擋。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def _vc_class():
    with patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
    return VoiceController


def _bypass(VC, *, playing=True, tts_text="", now=1000.0, cooldown=0.0,
           dom="task", conf=0.557):
    return VC._intent_bypass_echo(playing, tts_text, now, cooldown, dom, conf)


def test_real_case_task_dominant_bypasses():
    """「麻煩播放我不想下單」total=0.557 dom=task → 放行。"""
    VC = _vc_class()
    assert _bypass(VC, dom="task", conf=0.557) is True


def test_real_case_control_dominant_bypasses():
    """「幫我下一首」total=0.569 dom=control → 放行。"""
    VC = _vc_class()
    assert _bypass(VC, dom="control", conf=0.569) is True


def test_chitchat_case_stays_blocked():
    """同時段閒聊 total=0.346 dom=task → 繼續擋。"""
    VC = _vc_class()
    assert _bypass(VC, dom="task", conf=0.346) is False


def test_confidence_at_threshold_bypasses():
    """0.5 邊界值 → 放行。"""
    VC = _vc_class()
    assert _bypass(VC, conf=0.5) is True


def test_confidence_just_below_threshold_blocked():
    """0.49 差一點 → 不繞。"""
    VC = _vc_class()
    assert _bypass(VC, conf=0.49) is False


def test_voice_dominant_no_bypass():
    """dom=voice 歸 strong-voice bypass 管，這裡不放行。"""
    VC = _vc_class()
    assert _bypass(VC, dom="voice", conf=0.9) is False


def test_info_dominant_no_bypass():
    """dom=info 不在 control/task 放行清單內。"""
    VC = _vc_class()
    assert _bypass(VC, dom="info", conf=0.9) is False


def test_not_playing_no_bypass():
    VC = _vc_class()
    assert _bypass(VC, playing=False) is False


def test_tts_active_no_bypass():
    VC = _vc_class()
    assert _bypass(VC, tts_text="馬文正在講話") is False


def test_in_tts_cooldown_no_bypass():
    VC = _vc_class()
    assert _bypass(VC, now=1000.0, cooldown=1001.5) is False


def test_legacy_path_none_confidence_no_bypass():
    VC = _vc_class()
    assert _bypass(VC, conf=None) is False
