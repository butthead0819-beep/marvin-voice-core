"""TDD — Echo Guard 強人聲 bypass（放歌中也能語音點歌；零鍵盤核心）。

舊邏輯：is_playing_audio 時所有 fast wake 一律當回授抑制 → 開台持續放歌時
喚醒點歌沒一次成功（2026-06-04 觀察）。

修法：純音樂播放中（非 TTS 回授窗）的強人聲喚醒放行。嚴格防自我觸發——
bot 正在講 TTS（_current_tts_text 非空）或 TTS 後冷卻窗內一律不繞；只在
voice 主導 + voice 分數高 + 總信心高時放行。legacy 路徑（無 fusion 分數）不繞。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def _vc_class():
    with patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
    return VoiceController


def _bypass(VC, *, playing=True, tts_text="", now=1000.0, cooldown=0.0,
           dom="voice", conf=0.7, voice=1.0):
    return VC._strong_voice_bypass_echo(playing, tts_text, now, cooldown, dom, conf, voice)


def test_strong_voice_during_music_bypasses():
    """關鍵修復：純音樂播放中的強人聲喚醒（v=1.0 dom=voice total=0.7）放行。"""
    VC = _vc_class()
    assert _bypass(VC) is True


def test_not_playing_no_bypass():
    """沒在播放 → 本就不在 echo window，bypass 無意義回 False。"""
    VC = _vc_class()
    assert _bypass(VC, playing=False) is False


def test_tts_active_no_bypass():
    """bot 正在講 TTS（_current_tts_text 非空）→ 真回授風險，不繞（防自我觸發）。"""
    VC = _vc_class()
    assert _bypass(VC, tts_text="馬文正在講話") is False


def test_in_tts_cooldown_no_bypass():
    """TTS 後 2s 冷卻窗內（now < cooldown）→ 不繞。"""
    VC = _vc_class()
    assert _bypass(VC, now=1000.0, cooldown=1001.5) is False


def test_weak_confidence_no_bypass():
    """總信心不夠高（0.4 < 0.55）→ 不繞，維持抑制。"""
    VC = _vc_class()
    assert _bypass(VC, conf=0.4) is False


def test_non_voice_dominant_no_bypass():
    """非 voice 主導（task）→ 不繞。"""
    VC = _vc_class()
    assert _bypass(VC, dom="task") is False


def test_low_voice_score_no_bypass():
    """voice channel 分數不夠高（0.6 < 0.9）→ 不繞。"""
    VC = _vc_class()
    assert _bypass(VC, voice=0.6) is False


def test_legacy_path_none_confidence_no_bypass():
    """legacy 路徑無 fusion 分數（confidence=None）→ 不繞（安全預設）。"""
    VC = _vc_class()
    assert _bypass(VC, conf=None) is False
