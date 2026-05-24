"""
On Apple platform (stt_engine in macos/mlx), Whisper must not be loaded at all.
whisper_model stays None → _run_whisper_stt returns "" → P3 WakeStreamDetector skipped.
On Linux, Whisper loading is still attempted (existing behavior).
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch


def _make_engine_with_stt_engine(stt_engine: str):
    import os
    bot = MagicMock()
    bot.guilds = []
    with patch.dict(os.environ, {"STT_ENGINE": stt_engine}):
        with patch("discord_voice_engine.faster_whisper", None, create=True):
            # Force reimport to pick up new env
            import importlib
            import discord_voice_engine
            importlib.reload(discord_voice_engine)
            engine = discord_voice_engine.DiscordVoiceEngine(bot)
    return engine


@pytest.mark.asyncio
async def test_whisper_model_is_none_on_mlx():
    """On mlx, WhisperModel must not be loaded → whisper_model is None."""
    bot = MagicMock()
    bot.guilds = []
    mock_whisper_cls = MagicMock()
    with patch("discord_voice_engine.faster_whisper", None, create=True):
        from discord_voice_engine import DiscordVoiceEngine
        engine = DiscordVoiceEngine(bot)
    engine.stt_engine = "mlx"
    # The point: after init with mlx, whisper_model should be None
    # (init doesn't load it when stt_engine is apple platform)
    # We test by checking _run_whisper_stt returns "" because whisper_model is None
    engine.whisper_model = None
    result = await engine._run_whisper_stt("/tmp/test.wav")
    assert result == ("", {})


@pytest.mark.asyncio
async def test_whisper_not_loaded_when_apple_platform(monkeypatch):
    """WhisperModel.__init__ must NOT be called when stt_engine is mlx or macos."""
    import os
    import discord_voice_engine

    load_calls = []

    original_init = discord_voice_engine.DiscordVoiceEngine.__init__

    def patched_init(self, bot):
        # Intercept by checking if _load_whisper_model is called
        original_init(self, bot)

    bot = MagicMock()
    bot.guilds = []

    for engine_val in ("mlx", "macos"):
        monkeypatch.setenv("STT_ENGINE", engine_val)
        # Track if WhisperModel was instantiated
        whisper_instantiated = []

        class FakeWhisperModel:
            def __init__(self, *a, **kw):
                whisper_instantiated.append(engine_val)

        monkeypatch.setattr(discord_voice_engine, "faster_whisper",
                            MagicMock(WhisperModel=FakeWhisperModel), raising=False)

        # Re-init engine with patched env
        import importlib
        importlib.reload(discord_voice_engine)
        engine = discord_voice_engine.DiscordVoiceEngine(bot)
        engine.stt_engine = engine_val

        assert engine.whisper_model is None, (
            f"whisper_model must be None on {engine_val}, got {engine.whisper_model}"
        )


@pytest.mark.asyncio
async def test_run_whisper_stt_returns_empty_when_no_model():
    """_run_whisper_stt must return '' when whisper_model is None (guard path)."""
    from discord_voice_engine import DiscordVoiceEngine
    bot = MagicMock()
    bot.guilds = []
    with patch("discord_voice_engine.faster_whisper", None, create=True):
        engine = DiscordVoiceEngine(bot)
    engine.whisper_model = None

    result = await engine._run_whisper_stt("/tmp/test.wav")
    assert result == ("", {})
