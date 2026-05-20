"""
Tests for wake_check Groq fallback on Apple platforms (macos / mlx).

Context: 5/18 20:28 incident — Swift STT 連續 35× EDEADLK (macOS file I/O
under memory pressure)，wake_check 完全沒 fallback，整段 4 分鐘 session
0 個 wake 觸發。

Constraint (from 7cbc32e): Whisper must NEVER run in wake_check on Apple
to prevent zombie threads. Groq is HTTP-based, no local thread/subprocess,
safe to fall back to.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_engine(stt_engine: str = "mlx"):
    bot = MagicMock()
    bot.guilds = []
    with patch("discord_voice_engine.faster_whisper", None, create=True):
        from discord_voice_engine import DiscordVoiceEngine
        engine = DiscordVoiceEngine(bot)
    engine.stt_engine = stt_engine
    engine.whisper_model = MagicMock()
    return engine


@pytest.mark.asyncio
async def test_wake_check_falls_back_to_groq_when_swift_empty_on_mlx(monkeypatch):
    """Swift returns empty (EDEADLK 場景) → Groq fallback fires on mlx."""
    monkeypatch.setenv("GROQ_API_KEY", "test_key")
    monkeypatch.delenv("STT_SWIFT_STRICT", raising=False)  # 確保非 strict 模式測 fallback
    engine = _make_engine(stt_engine="mlx")
    engine._run_swift_stt = AsyncMock(return_value="")
    engine._run_groq_whisper_stt = AsyncMock(return_value="嗨馬文")
    engine._run_whisper_stt = AsyncMock(return_value="should not be called")

    await engine._process_stt_hybrid(
        speaker_name="狗與露",
        wav_path="/tmp/test.wav",
        wav_bytes=b"",
        timestamp=0.0,
        is_wake_check=True,
    )

    engine._run_groq_whisper_stt.assert_called_once()
    engine._run_whisper_stt.assert_not_called()  # zombie thread prevention preserved


@pytest.mark.asyncio
async def test_wake_check_falls_back_to_groq_when_swift_empty_on_macos(monkeypatch):
    """Same fallback on stt_engine=='macos'."""
    monkeypatch.setenv("GROQ_API_KEY", "test_key")
    monkeypatch.delenv("STT_SWIFT_STRICT", raising=False)
    engine = _make_engine(stt_engine="macos")
    engine._run_swift_stt = AsyncMock(return_value="")
    engine._run_groq_whisper_stt = AsyncMock(return_value="嗨馬文")
    engine._run_whisper_stt = AsyncMock(return_value="should not be called")

    await engine._process_stt_hybrid(
        speaker_name="狗與露",
        wav_path="/tmp/test.wav",
        wav_bytes=b"",
        timestamp=0.0,
        is_wake_check=True,
    )

    engine._run_groq_whisper_stt.assert_called_once()
    engine._run_whisper_stt.assert_not_called()


@pytest.mark.asyncio
async def test_wake_check_skips_groq_when_swift_succeeds(monkeypatch):
    """Swift returns text → Groq must NOT be called (don't waste API quota)."""
    monkeypatch.setenv("GROQ_API_KEY", "test_key")
    engine = _make_engine(stt_engine="mlx")
    engine._run_swift_stt = AsyncMock(return_value="嗨馬文")
    engine._run_groq_whisper_stt = AsyncMock(return_value="should not be called")
    engine._run_whisper_stt = AsyncMock(return_value="should not be called")

    await engine._process_stt_hybrid(
        speaker_name="狗與露",
        wav_path="/tmp/test.wav",
        wav_bytes=b"",
        timestamp=0.0,
        is_wake_check=True,
    )

    engine._run_groq_whisper_stt.assert_not_called()
    engine._run_whisper_stt.assert_not_called()


@pytest.mark.asyncio
async def test_wake_check_no_fallback_when_groq_key_missing(monkeypatch):
    """No GROQ_API_KEY → fall back silently to original Swift-only behavior."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    engine = _make_engine(stt_engine="mlx")
    engine._run_swift_stt = AsyncMock(return_value="")
    engine._run_groq_whisper_stt = AsyncMock(return_value="should not be called")
    engine._run_whisper_stt = AsyncMock(return_value="should not be called")

    await engine._process_stt_hybrid(
        speaker_name="狗與露",
        wav_path="/tmp/test.wav",
        wav_bytes=b"",
        timestamp=0.0,
        is_wake_check=True,
    )

    engine._run_groq_whisper_stt.assert_not_called()
    engine._run_whisper_stt.assert_not_called()


@pytest.mark.asyncio
async def test_wake_check_strict_mode_skips_groq_fallback(monkeypatch):
    """STT_SWIFT_STRICT=true → Swift empty 時 Groq 也不該被呼叫（防 Whisper 幻覺）。
    2026-05-20 引入：解 Groq Whisper 在低訊號 wake check 幻覺中文名字問題。"""
    monkeypatch.setenv("GROQ_API_KEY", "test_key")
    monkeypatch.setenv("STT_SWIFT_STRICT", "true")
    engine = _make_engine(stt_engine="mlx")
    engine._run_swift_stt = AsyncMock(return_value="")  # Swift 空
    engine._run_groq_whisper_stt = AsyncMock(return_value="嗨馬文")

    await engine._process_stt_hybrid(
        speaker_name="狗與露",
        wav_path="/tmp/test.wav",
        wav_bytes=b"",
        timestamp=0.0,
        is_wake_check=True,
    )

    engine._run_groq_whisper_stt.assert_not_called()  # STRICT 模式下 Groq 不該跑


@pytest.mark.asyncio
async def test_wake_check_groq_failure_returns_empty(monkeypatch):
    """Swift empty + Groq also returns empty → wake_check returns empty (no crash)."""
    monkeypatch.setenv("GROQ_API_KEY", "test_key")
    monkeypatch.delenv("STT_SWIFT_STRICT", raising=False)
    engine = _make_engine(stt_engine="mlx")
    engine._run_swift_stt = AsyncMock(return_value="")
    engine._run_groq_whisper_stt = AsyncMock(return_value="")
    engine._run_whisper_stt = AsyncMock(return_value="should not be called")

    # Must not raise
    await engine._process_stt_hybrid(
        speaker_name="狗與露",
        wav_path="/tmp/test.wav",
        wav_bytes=b"",
        timestamp=0.0,
        is_wake_check=True,
    )

    engine._run_groq_whisper_stt.assert_called_once()
    engine._run_whisper_stt.assert_not_called()
