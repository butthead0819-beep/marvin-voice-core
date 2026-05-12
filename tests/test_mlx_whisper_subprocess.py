"""
Tests for MLX Whisper subprocess wrapper.

mlx_whisper_bin.py: standalone script, reads WAV path from argv, prints result to stdout.
_run_mlx_whisper_stt: async method, spawns subprocess, kills on timeout (no zombie threads).

NOT wired into _process_stt_hybrid yet — infrastructure only.
"""
from __future__ import annotations

import asyncio
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call


def _make_engine():
    bot = MagicMock()
    bot.guilds = []
    with patch("discord_voice_engine.faster_whisper", None, create=True):
        from discord_voice_engine import DiscordVoiceEngine
        engine = DiscordVoiceEngine(bot)
    return engine


def _make_fake_process(stdout_bytes: bytes, returncode: int = 0):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout_bytes, b""))
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = MagicMock()
    return proc


# ---------------------------------------------------------------------------
# _run_mlx_whisper_stt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mlx_whisper_returns_text_on_success():
    """Returns transcribed text when subprocess exits 0 and prints text."""
    engine = _make_engine()
    proc = _make_fake_process(b"\xe4\xbd\xa0\xe5\xa5\xbd\xe9\xa6\xac\xe6\x96\x87\n")  # 你好馬文

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        result = await engine._run_mlx_whisper_stt("/tmp/test.wav")

    assert result == "你好馬文"


@pytest.mark.asyncio
async def test_mlx_whisper_returns_empty_on_nonzero_exit():
    """Returns '' when subprocess exits with non-zero code."""
    engine = _make_engine()
    proc = _make_fake_process(b"", returncode=1)

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        result = await engine._run_mlx_whisper_stt("/tmp/test.wav")

    assert result == ""


@pytest.mark.asyncio
async def test_mlx_whisper_kills_process_on_timeout():
    """On timeout, process.kill() is called — no zombie subprocess."""
    engine = _make_engine()

    proc = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=-9)

    async def slow_communicate():
        await asyncio.sleep(60)
        return b"", b""

    proc.communicate = slow_communicate

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        result = await engine._run_mlx_whisper_stt("/tmp/test.wav")

    assert result == ""
    proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_mlx_whisper_returns_empty_on_empty_stdout():
    """Returns '' when subprocess exits 0 but prints nothing."""
    engine = _make_engine()
    proc = _make_fake_process(b"   \n", returncode=0)

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        result = await engine._run_mlx_whisper_stt("/tmp/test.wav")

    assert result == ""


@pytest.mark.asyncio
async def test_mlx_whisper_subprocess_receives_wav_path():
    """Subprocess is called with the correct wav path as argument."""
    engine = _make_engine()
    proc = _make_fake_process(b"text\n")
    mock_exec = AsyncMock(return_value=proc)

    with patch("asyncio.create_subprocess_exec", mock_exec):
        await engine._run_mlx_whisper_stt("/tmp/audio.wav")

    args = mock_exec.call_args[0]
    assert "/tmp/audio.wav" in args


@pytest.mark.asyncio
async def test_mlx_whisper_strips_whitespace():
    """Strips leading/trailing whitespace from stdout."""
    engine = _make_engine()
    proc = _make_fake_process(b"  \xe6\xb8\xac\xe8\xa9\xa6  \n")  # "  測試  \n"

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        result = await engine._run_mlx_whisper_stt("/tmp/test.wav")

    assert result == "測試"


# ---------------------------------------------------------------------------
# mlx_whisper_bin.py — script-level tests
# ---------------------------------------------------------------------------


def test_mlx_whisper_bin_exists():
    """mlx_whisper_bin.py must exist at project root."""
    import os
    assert os.path.exists("mlx_whisper_bin.py"), "mlx_whisper_bin.py not found"


def test_mlx_whisper_bin_exits_nonzero_without_args(tmp_path):
    """Script exits non-zero when called without a wav path argument."""
    import subprocess
    result = subprocess.run(
        [sys.executable, "mlx_whisper_bin.py"],
        capture_output=True,
    )
    assert result.returncode != 0


def test_mlx_whisper_bin_exits_nonzero_for_missing_file(tmp_path):
    """Script exits non-zero when the wav file doesn't exist."""
    import subprocess
    result = subprocess.run(
        [sys.executable, "mlx_whisper_bin.py", "/nonexistent/path.wav"],
        capture_output=True,
    )
    assert result.returncode != 0
