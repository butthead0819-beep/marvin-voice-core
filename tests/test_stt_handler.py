"""Tests for STTHandler — Protocol adapter and hybrid transcription logic."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from marvin_voice_core.stt_handler import STTHandler
from protocols import STTService


# ── Protocol conformance ──────────────────────────────────────────────────────

def test_stt_handler_satisfies_protocol():
    handler = STTHandler(whisper_model=None)
    assert isinstance(handler, STTService)


# ── transcribe() Protocol entry point ────────────────────────────────────────

@pytest.mark.asyncio
async def test_transcribe_delegates_to_transcribe_hybrid(tmp_path):
    handler = STTHandler(whisper_model=None)
    wav = str(tmp_path / "test.wav")
    open(wav, "wb").close()   # empty wav file — swift will fail, that's fine

    with patch.object(handler, "transcribe_hybrid", new=AsyncMock(return_value=("馬文你好", "Mock"))) as m:
        text, engine = await handler.transcribe(wav, speaker="Alice", context="test_ctx")

    m.assert_awaited_once_with(wav, speaker_name="Alice", game_dict_string="test_ctx")
    assert text == "馬文你好"
    assert engine == "Mock"


# ── transcribe_hybrid: Swift success path ────────────────────────────────────

@pytest.mark.asyncio
async def test_transcribe_hybrid_uses_swift_result(tmp_path):
    handler = STTHandler(whisper_model=None)
    wav = str(tmp_path / "audio.wav")

    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.communicate = AsyncMock(return_value=(b"Hello Swift\n", b""))

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_process)):
        text, engine = await handler.transcribe_hybrid(wav, speaker_name="Bob")

    assert text == "Hello Swift"
    assert engine == "Swift"


@pytest.mark.asyncio
async def test_transcribe_hybrid_skips_debug_lines(tmp_path):
    handler = STTHandler(whisper_model=None)
    wav = str(tmp_path / "audio.wav")

    # Lines starting with 🔍, ✅, ❌, DEBUG:, 📚 should be skipped
    output = b"DEBUG: something\n\xe2\x9c\x85 ok\nActual transcript\n"
    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.communicate = AsyncMock(return_value=(output, b""))

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_process)):
        text, engine = await handler.transcribe_hybrid(wav, speaker_name="Carol")

    assert text == "Actual transcript"
    assert engine == "Swift"


# ── transcribe_hybrid: Swift failure → Whisper fallback ──────────────────────

@pytest.mark.asyncio
async def test_transcribe_hybrid_falls_back_to_whisper(tmp_path):
    handler = STTHandler(whisper_model=None)
    wav = str(tmp_path / "audio.wav")

    # Swift fails
    mock_process = MagicMock()
    mock_process.returncode = 1
    mock_process.communicate = AsyncMock(return_value=(b"", b"error"))

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_process)):
        text, engine = await handler.transcribe_hybrid(wav, speaker_name="Dave")

    # No whisper model → empty result
    assert text == ""
    assert engine == "None"


@pytest.mark.asyncio
async def test_transcribe_hybrid_whisper_used_when_swift_empty(tmp_path):
    # Mock whisper model
    seg = MagicMock()
    seg.text = "Whisper output"
    whisper_mock = MagicMock()
    whisper_mock.transcribe = MagicMock(return_value=(iter([seg]), None))

    handler = STTHandler(whisper_model=whisper_mock)
    wav = str(tmp_path / "audio.wav")

    # Swift returns no text
    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.communicate = AsyncMock(return_value=(b"\n", b""))

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_process)):
        text, engine = await handler.transcribe_hybrid(wav, speaker_name="Eve")

    assert text == "Whisper output"
    assert engine == "Whisper"


# ── hallucination filter (Fix 1) ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_swift_repeated_token_hallucination_is_dropped(tmp_path):
    """聽×30 type: Swift output that is pure token repetition → filtered to empty."""
    handler = STTHandler(whisper_model=None)
    wav = str(tmp_path / "audio.wav")

    hallucinated = "聽,聽,聽,聽,聽,聽,聽,聽,聽,聽,聽,聽,聽,聽,聽,聽,聽,聽,聽,聽,聽,聽,聽,聽,聽,聽,聽,聽,聽,聽"
    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.communicate = AsyncMock(return_value=(hallucinated.encode(), b""))

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_process)):
        text, engine = await handler.transcribe_hybrid(wav, speaker_name="Alice")

    assert text == ""
    assert engine == "None"


@pytest.mark.asyncio
async def test_swift_same_phrase_repeated_three_times_is_dropped(tmp_path):
    """Pattern 1: exact same token repeated ≥3 times → is_whisper_hallucination catches it."""
    handler = STTHandler(whisper_model=None)
    wav = str(tmp_path / "audio.wav")

    # 艾瑪文 repeated 3 times — set size == 1, len >= 3 → pattern 1
    hallucinated = "艾瑪文,艾瑪文,艾瑪文"
    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.communicate = AsyncMock(return_value=(hallucinated.encode("utf-8"), b""))

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_process)):
        text, engine = await handler.transcribe_hybrid(wav, speaker_name="Bob")

    assert text == ""
    assert engine == "None"


@pytest.mark.asyncio
async def test_swift_normal_output_not_filtered(tmp_path):
    """Normal transcription should pass through unchanged."""
    handler = STTHandler(whisper_model=None)
    wav = str(tmp_path / "audio.wav")

    normal = "馬文幫我查一下天氣"
    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.communicate = AsyncMock(return_value=(normal.encode("utf-8"), b""))

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_process)):
        text, engine = await handler.transcribe_hybrid(wav, speaker_name="Carol")

    assert text == normal
    assert engine == "Swift"


# ── transcribe_hybrid: Swift exception ───────────────────────────────────────

@pytest.mark.asyncio
async def test_transcribe_hybrid_handles_swift_exception(tmp_path):
    handler = STTHandler(whisper_model=None)
    wav = str(tmp_path / "audio.wav")

    with patch("asyncio.create_subprocess_exec", side_effect=OSError("swift not found")):
        text, engine = await handler.transcribe_hybrid(wav, speaker_name="Frank")

    assert text == ""
    assert engine == "None"
