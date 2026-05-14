"""
Tests for split STT inflight counters.

Problem: a single _stt_inflight counter blocks wake_check when full-STT is saturated.
Fix: separate _wake_inflight and _full_stt_inflight counters so each type has its own cap.

These tests guard the invariant:
  - full-STT saturation must NOT block wake_check
  - wake_check saturation must NOT block full-STT
  - each type's own cap still drops correctly
"""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_engine():
    bot = MagicMock()
    bot.guilds = []
    with patch("discord_voice_engine.faster_whisper", None, create=True):
        from discord_voice_engine import DiscordVoiceEngine
        engine = DiscordVoiceEngine(bot)
    return engine


def _inject_pcm(engine, user_id: int, seconds: float = 0.1):
    """Put a tiny PCM buffer into the engine so _flush doesn't bail on empty audio."""
    samples = int(48000 * seconds * 2)  # stereo int16
    import numpy as np
    pcm = (np.zeros(samples, dtype=np.int16) + 1000).tobytes()
    engine.audio_buffers[user_id] = {"pcm": bytearray(pcm), "first_start": 0.0}


# ---------------------------------------------------------------------------
# Guard: separate counter attributes exist
# ---------------------------------------------------------------------------

def test_engine_has_split_inflight_counters():
    """Engine must expose _wake_inflight and _full_stt_inflight (not the old merged _stt_inflight)."""
    engine = _make_engine()
    assert hasattr(engine, "_wake_inflight"), "missing _wake_inflight"
    assert hasattr(engine, "_full_stt_inflight"), "missing _full_stt_inflight"
    assert hasattr(engine, "_MAX_WAKE_INFLIGHT"), "missing _MAX_WAKE_INFLIGHT"
    assert hasattr(engine, "_MAX_FULL_STT_INFLIGHT"), "missing _MAX_FULL_STT_INFLIGHT"


# ---------------------------------------------------------------------------
# Core invariant: the two types don't block each other
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wake_check_not_blocked_by_full_stt_saturation():
    """When full-STT is at its cap, a wake_check must still be allowed to start."""
    engine = _make_engine()
    engine._full_stt_inflight = engine._MAX_FULL_STT_INFLIGHT  # saturate full-STT

    _inject_pcm(engine, user_id=1)
    started = False

    async def fake_process(*args, **kwargs):
        nonlocal started
        started = True

    engine._process_stt_hybrid = fake_process

    with patch.object(engine, "_run_swift_stt", AsyncMock(return_value="")):
        await engine._flush_audio_to_stt(user_id=1, is_wake_check=True)

    assert started, "wake_check was wrongly dropped because full-STT slots were full"


@pytest.mark.asyncio
async def test_full_stt_not_blocked_by_wake_check_saturation():
    """When wake_check is at its cap, a full-STT must still be allowed to start."""
    engine = _make_engine()
    engine._wake_inflight = engine._MAX_WAKE_INFLIGHT  # saturate wake

    _inject_pcm(engine, user_id=2)
    started = False

    async def fake_process(*args, **kwargs):
        nonlocal started
        started = True

    engine._process_stt_hybrid = fake_process

    with patch.object(engine, "_run_swift_stt", AsyncMock(return_value="")):
        await engine._flush_audio_to_stt(user_id=2, is_wake_check=False)

    assert started, "full-STT was wrongly dropped because wake_check slots were full"


# ---------------------------------------------------------------------------
# Each type's own cap still drops correctly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wake_check_dropped_when_wake_inflight_full():
    """When _wake_inflight is at its cap, a new wake_check is dropped."""
    engine = _make_engine()
    engine._wake_inflight = engine._MAX_WAKE_INFLIGHT

    _inject_pcm(engine, user_id=3)
    started = False

    async def fake_process(*args, **kwargs):
        nonlocal started
        started = True

    engine._process_stt_hybrid = fake_process

    await engine._flush_audio_to_stt(user_id=3, is_wake_check=True)

    assert not started, "wake_check should have been dropped when wake slots are full"


@pytest.mark.asyncio
async def test_full_stt_dropped_when_full_stt_inflight_full():
    """When _full_stt_inflight is at its cap, a new full-STT is dropped."""
    engine = _make_engine()
    engine._full_stt_inflight = engine._MAX_FULL_STT_INFLIGHT

    _inject_pcm(engine, user_id=4)
    started = False

    async def fake_process(*args, **kwargs):
        nonlocal started
        started = True

    engine._process_stt_hybrid = fake_process

    await engine._flush_audio_to_stt(user_id=4, is_wake_check=False)

    assert not started, "full-STT should have been dropped when full-STT slots are full"


# ---------------------------------------------------------------------------
# Counter lifecycle: increments and decrements correctly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wake_inflight_returns_to_zero_after_completion():
    """_wake_inflight is incremented during processing and decremented when done."""
    engine = _make_engine()
    _inject_pcm(engine, user_id=5)

    async def fake_process(*args, **kwargs):
        assert engine._wake_inflight == 1, "counter should be 1 during processing"

    engine._process_stt_hybrid = fake_process

    with patch.object(engine, "_run_swift_stt", AsyncMock(return_value="")):
        await engine._flush_audio_to_stt(user_id=5, is_wake_check=True)

    assert engine._wake_inflight == 0, "counter should be 0 after completion"


@pytest.mark.asyncio
async def test_full_stt_inflight_returns_to_zero_after_completion():
    """_full_stt_inflight is incremented during processing and decremented when done."""
    engine = _make_engine()
    _inject_pcm(engine, user_id=6)

    async def fake_process(*args, **kwargs):
        assert engine._full_stt_inflight == 1, "counter should be 1 during processing"

    engine._process_stt_hybrid = fake_process

    with patch.object(engine, "_run_swift_stt", AsyncMock(return_value="")):
        await engine._flush_audio_to_stt(user_id=6, is_wake_check=False)

    assert engine._full_stt_inflight == 0, "counter should be 0 after completion"


@pytest.mark.asyncio
async def test_wake_inflight_decrements_even_on_exception():
    """_wake_inflight is decremented in finally, even if processing raises."""
    engine = _make_engine()
    _inject_pcm(engine, user_id=7)

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated STT crash")

    engine._process_stt_hybrid = boom

    with patch.object(engine, "_run_swift_stt", AsyncMock(return_value="")):
        await engine._flush_audio_to_stt(user_id=7, is_wake_check=True)

    assert engine._wake_inflight == 0, "counter must decrement even after exception"
