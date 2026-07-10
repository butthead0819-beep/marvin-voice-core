"""
tests/test_music_echo_guard.py

TDD：軟體 Music Echo Guard——播純音樂時，讓 local/satellite（無硬體 AEC）忽略
衛星喚醒 + 不觸發 barge-in。喇叭外放的音樂被同機麥收回（回聲）不該當成人聲。

驗：
(A) 純函式 music_echo_guard_active 的真值表（enabled / local_mode / is_playing /
    current_tts_text 四軸）
(B) _on_satellite_wake：guard active → 不 duck；inactive → 照舊 duck
(C) handle_raw_speech_start：guard active → barge-in block 跳過；inactive → 照舊打斷
(D) Discord 路徑（_local_mode 不存在）恆不受影響
"""
from __future__ import annotations

from unittest.mock import MagicMock

from cogs.voice_controller import VoiceController
from cogs.voice_controller_connection import ConnectionMixin, music_echo_guard_active


# ── (A) 純函式真值表 ──────────────────────────────────────────────────────────

def test_guard_active_pure_music_on_local_path():
    # local/satellite + 播放中 + 無 TTS 文字（純音樂）+ 啟用 → active
    assert music_echo_guard_active(True, True, "", True) is True


def test_guard_inactive_when_disabled():
    assert music_echo_guard_active(True, True, "", False) is False


def test_guard_inactive_on_discord_path():
    # local_mode=False（Discord）→ 不受影響
    assert music_echo_guard_active(False, True, "", True) is False


def test_guard_inactive_when_not_playing():
    assert music_echo_guard_active(True, False, "", True) is False


def test_guard_inactive_during_tts():
    # bot 正在講 TTS（current_tts_text 非空）→ 使用者打斷 bot 說話仍要能 barge-in
    assert music_echo_guard_active(True, True, "未說完的話", True) is False


# ── (B) _on_satellite_wake：guard 控制 Pi 喚醒 duck ───────────────────────────

def _make_wake_self(*, local_mode=True, is_playing_audio=True, current_tts_text=""):
    fake = MagicMock()
    fake._local_mode = local_mode
    fake.is_playing_audio = is_playing_audio
    fake._current_tts_text = current_tts_text
    fake._mixer = MagicMock()
    return fake


def test_on_satellite_wake_skips_duck_during_pure_music(monkeypatch):
    monkeypatch.setenv("MARVIN_WAKE_DUCK", "1")
    monkeypatch.setenv("MARVIN_MUSIC_ECHO_GUARD", "1")
    fake = _make_wake_self()
    ConnectionMixin._on_satellite_wake(fake, "mawen_v1")
    fake._mixer.duck_for_wake.assert_not_called()


def test_on_satellite_wake_ducks_when_not_playing(monkeypatch):
    monkeypatch.setenv("MARVIN_WAKE_DUCK", "1")
    monkeypatch.setenv("MARVIN_MUSIC_ECHO_GUARD", "1")
    fake = _make_wake_self(is_playing_audio=False)
    ConnectionMixin._on_satellite_wake(fake, "mawen_v1")
    fake._mixer.duck_for_wake.assert_called_once()


def test_on_satellite_wake_ducks_when_echo_guard_killswitch_off(monkeypatch):
    monkeypatch.setenv("MARVIN_WAKE_DUCK", "1")
    monkeypatch.setenv("MARVIN_MUSIC_ECHO_GUARD", "0")
    fake = _make_wake_self()  # 純音樂，但 echo-guard 被關 → 照舊 duck
    ConnectionMixin._on_satellite_wake(fake, "mawen_v1")
    fake._mixer.duck_for_wake.assert_called_once()


# ── (C) handle_raw_speech_start：guard 控制 barge-in ──────────────────────────

def _make_barge_self(*, local_mode=True, current_tts_text=""):
    fake = MagicMock()
    fake.bot.cogs.get.return_value = None
    fake.bot.voice_clients = []
    fake.is_playing_audio = True
    fake._tts_protected = False
    fake._plan12 = True
    fake._local_mode = local_mode
    fake._mixer = MagicMock()
    fake._current_tts_text = current_tts_text
    fake._current_tts_in_channel = True
    fake.stt_logger = MagicMock()
    fake.last_marvin_speech_time = 0.0
    fake.user_states = {}
    fake.bot.engine.conv_buffer.get_conversation_temperature.return_value = 2.0
    return fake


# barge-in 硬停音樂＝永遠不該（純音樂只 duck，播放交命令流水線）→ 與 kill-switch 脫鉤。

def test_barge_in_skipped_during_pure_music_guard_on(monkeypatch):
    monkeypatch.setenv("MARVIN_MUSIC_ECHO_GUARD", "1")
    fake = _make_barge_self()  # local + 純音樂（無 TTS 文字）
    VoiceController.handle_raw_speech_start(fake, "Alice")
    fake._resolve_playback_device.assert_not_called()
    assert fake._tts_interrupted is not True


def test_barge_in_skipped_during_pure_music_even_when_killswitch_off(monkeypatch):
    # 關鍵回歸（2026-07-10 使用者實測「還是停掉了」）：kill-switch 關掉是為了 honor 喚醒 duck，
    # 但 barge-in 硬停音樂與那旗標無關，純音樂仍一律不該砍整首。
    monkeypatch.setenv("MARVIN_MUSIC_ECHO_GUARD", "0")
    fake = _make_barge_self()  # local 純音樂
    VoiceController.handle_raw_speech_start(fake, "Alice")
    fake._resolve_playback_device.assert_not_called()
    assert fake._tts_interrupted is not True


def test_barge_in_proceeds_on_discord_during_music(monkeypatch):
    monkeypatch.setenv("MARVIN_MUSIC_ECHO_GUARD", "0")
    fake = _make_barge_self(local_mode=False)  # Discord（_local_mode 不存在）→ 不受影響
    device = MagicMock()
    device.is_playing.return_value = True
    fake._resolve_playback_device.return_value = device
    VoiceController.handle_raw_speech_start(fake, "Alice")
    fake._resolve_playback_device.assert_called_once()
    device.stop.assert_called_once()
    assert fake._tts_interrupted is True


def test_barge_in_proceeds_during_tts_on_local(monkeypatch):
    # device 上 bot 正講 TTS（_current_tts_text 非空）→ 使用者打斷 bot 仍要中斷（非純音樂）。
    monkeypatch.setenv("MARVIN_MUSIC_ECHO_GUARD", "0")
    fake = _make_barge_self(current_tts_text="說到一半的回應")
    device = MagicMock()
    device.is_playing.return_value = True
    fake._resolve_playback_device.return_value = device
    VoiceController.handle_raw_speech_start(fake, "Alice")
    fake._resolve_playback_device.assert_called_once()
    assert fake._tts_interrupted is True
