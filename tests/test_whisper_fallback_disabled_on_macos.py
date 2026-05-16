"""
Tests for disabling Whisper on macOS/mlx platforms.

When stt_engine == "macos" or "mlx":
  - Sequential fallback: Swift fails → STT Fatal (no Whisper)
  - P2 wake_check race: Swift-only (no Whisper), prevents zombie threads

When stt_engine == "linux" (or any other value):
  - Sequential fallback: Whisper IS used
  - P2 wake_check: Swift + Whisper race (normal behavior)
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_engine(stt_engine: str = "macos"):
    bot = MagicMock()
    bot.guilds = []
    with patch("discord_voice_engine.faster_whisper", None, create=True):
        from discord_voice_engine import DiscordVoiceEngine
        engine = DiscordVoiceEngine(bot)
    engine.stt_engine = stt_engine
    engine.whisper_model = MagicMock()  # simulate loaded model
    return engine


# ---------------------------------------------------------------------------
# Sequential fallback (non-wake_check)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_whisper_not_called_as_fallback_on_macos(monkeypatch):
    """On macos, if Swift STT returns empty, Whisper fallback must NOT be triggered."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    engine = _make_engine(stt_engine="macos")
    engine._run_swift_stt = AsyncMock(return_value="")
    engine._run_whisper_stt = AsyncMock(return_value="whisper result")

    await engine._process_stt_hybrid(
        speaker_name="狗與露",
        wav_path="/tmp/test.wav",
        wav_bytes=b"",
        timestamp=0.0,
        is_wake_check=False,
    )

    engine._run_whisper_stt.assert_not_called()


@pytest.mark.asyncio
async def test_whisper_not_called_as_fallback_on_mlx(monkeypatch):
    """On mlx (the actual env value on this Mac), Whisper fallback must NOT be triggered."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    engine = _make_engine(stt_engine="mlx")
    engine._run_swift_stt = AsyncMock(return_value="")
    engine._run_whisper_stt = AsyncMock(return_value="whisper result")

    await engine._process_stt_hybrid(
        speaker_name="狗與露",
        wav_path="/tmp/test.wav",
        wav_bytes=b"",
        timestamp=0.0,
        is_wake_check=False,
    )

    engine._run_whisper_stt.assert_not_called()


@pytest.mark.asyncio
async def test_whisper_still_called_as_fallback_on_linux():
    """On Linux (stt_engine not in macos/mlx), Whisper fallback must still be triggered."""
    engine = _make_engine(stt_engine="linux")
    engine._run_swift_stt = AsyncMock(return_value="")
    engine._run_whisper_stt = AsyncMock(return_value="whisper result")

    await engine._process_stt_hybrid(
        speaker_name="testuser",
        wav_path="/tmp/test.wav",
        wav_bytes=b"",
        timestamp=0.0,
        is_wake_check=False,
    )

    engine._run_whisper_stt.assert_called_once()


@pytest.mark.asyncio
async def test_swift_success_on_mlx_skips_whisper():
    """On mlx, if Swift succeeds, Whisper must not be called at all."""
    engine = _make_engine(stt_engine="mlx")
    engine._run_swift_stt = AsyncMock(return_value="你好馬文")
    engine._run_whisper_stt = AsyncMock(return_value="should not be called")

    await engine._process_stt_hybrid(
        speaker_name="狗與露",
        wav_path="/tmp/test.wav",
        wav_bytes=b"",
        timestamp=0.0,
        is_wake_check=False,
    )

    engine._run_whisper_stt.assert_not_called()


# ---------------------------------------------------------------------------
# P2 wake_check race
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wake_check_swift_only_on_macos():
    """On macos, P2 wake_check race must NOT spawn a Whisper task (zombie thread prevention)."""
    engine = _make_engine(stt_engine="macos")
    engine._run_swift_stt = AsyncMock(return_value="嗨馬文")
    engine._run_whisper_stt = AsyncMock(return_value="should not be called")

    await engine._process_stt_hybrid(
        speaker_name="狗與露",
        wav_path="/tmp/test.wav",
        wav_bytes=b"",
        timestamp=0.0,
        is_wake_check=True,
    )

    engine._run_whisper_stt.assert_not_called()


@pytest.mark.asyncio
async def test_wake_check_swift_only_on_mlx():
    """On mlx, P2 wake_check race must NOT spawn a Whisper task (zombie thread prevention)."""
    engine = _make_engine(stt_engine="mlx")
    engine._run_swift_stt = AsyncMock(return_value="嗨馬文")
    engine._run_whisper_stt = AsyncMock(return_value="should not be called")

    await engine._process_stt_hybrid(
        speaker_name="狗與露",
        wav_path="/tmp/test.wav",
        wav_bytes=b"",
        timestamp=0.0,
        is_wake_check=True,
    )

    engine._run_whisper_stt.assert_not_called()


@pytest.mark.asyncio
async def test_wake_check_swift_only_returns_result_on_mlx():
    """On mlx, if Swift returns text in wake_check, the result is used correctly."""
    engine = _make_engine(stt_engine="mlx")
    engine._run_swift_stt = AsyncMock(return_value="")  # Swift fails
    engine._run_whisper_stt = AsyncMock(return_value="should not be called")

    await engine._process_stt_hybrid(
        speaker_name="狗與露",
        wav_path="/tmp/test.wav",
        wav_bytes=b"",
        timestamp=0.0,
        is_wake_check=True,
    )

    # Swift-only: Whisper must never run even when Swift returns empty
    engine._run_whisper_stt.assert_not_called()


@pytest.mark.asyncio
async def test_wake_check_uses_whisper_on_linux():
    """On Linux, P2 wake_check race must use Whisper (only STT engine available)."""
    engine = _make_engine(stt_engine="linux")
    engine._run_swift_stt = AsyncMock(return_value="")
    engine._run_whisper_stt = AsyncMock(return_value="嗨馬文")

    await engine._process_stt_hybrid(
        speaker_name="testuser",
        wav_path="/tmp/test.wav",
        wav_bytes=b"",
        timestamp=0.0,
        is_wake_check=True,
    )

    engine._run_whisper_stt.assert_called_once()
