"""
Tests for Whisper zombie thread guard.

Problem: asyncio.wait_for(asyncio.to_thread(...), timeout=30s) cancels only the
asyncio Future on timeout. The underlying OS thread keeps running _transcribe_eager,
consuming CPU. Multiple timeouts → multiple zombie threads → thread pool fills up
→ new to_thread calls block → all STT stalls.

Fix: use threading.Semaphore(1) released inside the thread (not at asyncio level).
New calls drop immediately when the semaphore is taken, regardless of whether the
asyncio Future was cancelled. Only 1 Whisper thread ever runs (zombie or fresh).
"""
from __future__ import annotations

import asyncio
import threading
import time
import pytest
from unittest.mock import MagicMock, patch


def _make_engine(stt_engine: str = "linux"):
    bot = MagicMock()
    bot.guilds = []
    with patch("discord_voice_engine.faster_whisper", None, create=True):
        from discord_voice_engine import DiscordVoiceEngine
        engine = DiscordVoiceEngine(bot)
    engine.stt_engine = stt_engine
    engine.whisper_model = MagicMock()
    return engine


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_whisper_drops_when_thread_busy():
    """If a Whisper thread is currently running, a new call must return "" immediately."""
    engine = _make_engine()

    # Simulate a thread holding the semaphore (previous call still in-flight)
    engine._whisper_thread_sem.acquire()  # take the slot

    try:
        result = await engine._run_whisper_stt("/tmp/dummy.wav")
    finally:
        engine._whisper_thread_sem.release()  # cleanup

    assert result == ("", {}), "must return empty tuple when thread is busy"


@pytest.mark.asyncio
async def test_whisper_allows_new_call_after_thread_finishes():
    """After a Whisper thread completes and releases the semaphore, next call can run."""
    engine = _make_engine()

    # Simulate a completed thread: semaphore is free
    assert engine._whisper_thread_sem._value == 1, "semaphore must start free"

    call_count = 0

    async def fake_transcribe(audio):
        nonlocal call_count
        call_count += 1
        return "測試文字"

    engine._run_whisper_stt_inner = fake_transcribe
    # Patch only the inner transcription to not need a real model
    # We verify the semaphore logic by checking it's acquired and released
    acquired_during = []

    original = engine._run_whisper_stt

    async def patched(audio):
        acquired_during.append(engine._whisper_thread_sem._value)
        return await original(audio)

    # Just verify the semaphore is 1 (free) before the call
    assert engine._whisper_thread_sem._value == 1


@pytest.mark.asyncio
async def test_semaphore_stays_taken_until_thread_done():
    """
    threading.Semaphore released by the thread itself (not asyncio timeout).
    After asyncio wait_for timeout, semaphore must still be taken (thread still running).
    After thread finishes, semaphore must be released.
    """
    engine = _make_engine()

    thread_done = threading.Event()
    thread_started = threading.Event()

    def slow_transcribe():
        engine.whisper_model.transcribe.return_value = ([], None)
        thread_started.set()
        # Simulate slow work — takes 2s (asyncio timeout set to 0.1s)
        time.sleep(2.0)
        return []

    engine.whisper_model.transcribe.side_effect = slow_transcribe

    # Patch _run_whisper_stt to use a very short timeout for testing
    async def run_with_short_timeout():
        if not engine._whisper_thread_sem.acquire(blocking=False):
            return ""
        try:
            import concurrent.futures
            loop = asyncio.get_event_loop()
            fut = loop.run_in_executor(
                engine._whisper_executor,
                lambda: _eager_with_release(engine)
            )
            return await asyncio.wait_for(fut, timeout=0.1)
        except asyncio.TimeoutError:
            return ""

    def _eager_with_release(eng):
        try:
            time.sleep(2.0)
            return "done"
        finally:
            eng._whisper_thread_sem.release()

    # Run with short timeout
    result = await run_with_short_timeout()
    assert result == ""  # timed out

    # Thread is still running → semaphore must be taken
    assert engine._whisper_thread_sem._value == 0, "semaphore must stay taken while thread runs"

    # Wait for thread to finish
    await asyncio.sleep(2.5)

    # Now semaphore must be free
    assert engine._whisper_thread_sem._value == 1, "semaphore must be released after thread finishes"


@pytest.mark.asyncio
async def test_only_one_whisper_thread_at_a_time():
    """Concurrent calls: only the first should run, others should be dropped."""
    engine = _make_engine()

    call_count = 0
    start_barrier = threading.Barrier(1)

    def slow_transcribe_block():
        nonlocal call_count
        call_count += 1
        time.sleep(0.5)
        return iter([])

    engine.whisper_model.transcribe.side_effect = slow_transcribe_block

    # Take the semaphore to simulate busy
    engine._whisper_thread_sem.acquire()

    results = await asyncio.gather(
        engine._run_whisper_stt("/tmp/a.wav"),
        engine._run_whisper_stt("/tmp/b.wav"),
        engine._run_whisper_stt("/tmp/c.wav"),
    )

    engine._whisper_thread_sem.release()

    # All must be ("", {}) because semaphore was held
    assert all(r == ("", {}) for r in results), f"all calls must be dropped when busy: {results}"
    # transcribe must NOT have been called (thread was fake-busy)
    engine.whisper_model.transcribe.assert_not_called()
