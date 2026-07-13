"""
STT_DEBUG_ARCHIVE 開關測試。

背景：process_audio_slice 每次都無條件封存一份時間戳 stt_debug_*.wav，
重度使用日堆到 ~700M/天。改成預設關閉，只有 STT_DEBUG_ARCHIVE=true 才封存；
last_stt_debug.wav 不受開關影響，永遠寫最後一段。

守的不變式：
  - 未設環境變數 → self.stt_debug_archive is False（封存關閉）
  - STT_DEBUG_ARCHIVE=true → True
  - 其他值（如 "0"/"false"）→ False
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def _make_engine():
    bot = MagicMock()
    bot.guilds = []
    bot.cogs.get.return_value = None
    with patch("discord_voice_engine.faster_whisper", None, create=True):
        from discord_voice_engine import DiscordVoiceEngine
        engine = DiscordVoiceEngine(bot)
    return engine


def test_archive_defaults_off_when_env_unset(monkeypatch):
    monkeypatch.delenv("STT_DEBUG_ARCHIVE", raising=False)
    engine = _make_engine()
    assert engine.stt_debug_archive is False


def test_archive_on_when_env_true(monkeypatch):
    monkeypatch.setenv("STT_DEBUG_ARCHIVE", "true")
    engine = _make_engine()
    assert engine.stt_debug_archive is True


def test_archive_off_for_falsey_values(monkeypatch):
    for val in ("0", "false", "no", ""):
        monkeypatch.setenv("STT_DEBUG_ARCHIVE", val)
        engine = _make_engine()
        assert engine.stt_debug_archive is False, f"{val!r} 應視為關閉"
