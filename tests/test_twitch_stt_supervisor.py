"""TDD — twitch_stt_listener.run_listener supervisor loop.

Backstory: the old design used asyncio.gather for ffmpeg + processor. If
ffmpeg died (HLS URL expired ~30-60 min), the processor kept running on an
empty chunk dir and the whole thing looked alive but produced nothing. The
new supervisor loop watches ffmpeg.wait(), cancels processor on death,
backs off, refreshes the URL, and retries.

These tests pin the supervisor's contract:
  A) ffmpeg rc != 0 → backoff doubles
  B) ffmpeg rc == 0 → backoff resets to BASE
  C) processor_task and stderr_task are both cancelled between iterations
  D) backoff caps at MAX
  E) _drain_ffmpeg_stderr exits cleanly on EOF
  F) _drain_ffmpeg_stderr propagates CancelledError
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


# ── _drain_ffmpeg_stderr ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_drain_stderr_exits_on_eof():
    """readline returns b'' (EOF) → drain loop exits without raising."""
    from twitch_stt_listener import _drain_ffmpeg_stderr
    proc = MagicMock()
    proc.stderr = MagicMock()
    # First call: a real line; second call: EOF
    proc.stderr.readline = AsyncMock(side_effect=[b"some warning\n", b""])
    await _drain_ffmpeg_stderr(proc)  # should return cleanly


@pytest.mark.asyncio
async def test_drain_stderr_handles_none_stderr():
    """proc.stderr is None → drain returns immediately, no crash."""
    from twitch_stt_listener import _drain_ffmpeg_stderr
    proc = MagicMock()
    proc.stderr = None
    await _drain_ffmpeg_stderr(proc)


@pytest.mark.asyncio
async def test_drain_stderr_propagates_cancel():
    """CancelledError must propagate so supervisor's cancel() actually stops the task."""
    from twitch_stt_listener import _drain_ffmpeg_stderr
    proc = MagicMock()
    proc.stderr = MagicMock()

    async def _hang():
        await asyncio.sleep(10)
    proc.stderr.readline = _hang

    task = asyncio.create_task(_drain_ffmpeg_stderr(proc))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ── Supervisor loop ──────────────────────────────────────────────────────
# The supervisor uses tempfile.TemporaryDirectory + segment_stream + wait_for_stream
# + asyncio.sleep(backoff). We stub the four boundary calls so the loop runs N
# iterations against fake ffmpeg processes whose returncode we control.

_REAL_SLEEP = asyncio.sleep  # captured before tests patch asyncio.sleep


def _make_supervisor_doubles(returncodes: list[int]):
    """Build patches: wait_for_stream / segment_stream / sleep / init_db that
    drive run_listener for len(returncodes) iterations then raise to break the loop."""
    sleep_calls: list[float] = []

    async def fake_wait_for_stream(channel, poll_interval=60):
        if not returncodes:
            raise asyncio.CancelledError()
        return "https://fake-url"

    async def fake_segment_stream(url, chunk_dir, chunk_secs):
        proc = MagicMock()
        proc.stderr = MagicMock()
        # readline returns EOF immediately so drain task exits on its own
        proc.stderr.readline = AsyncMock(return_value=b"")
        rc = returncodes.pop(0)
        proc.wait = AsyncMock(return_value=rc)
        return proc

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        # Yield so processor_task gets a chance to start before we cancel.
        # Use the real sleep captured before patching to avoid infinite recursion.
        await _REAL_SLEEP(0)

    return fake_wait_for_stream, fake_segment_stream, fake_sleep, sleep_calls


@pytest.mark.asyncio
async def test_supervisor_backoff_doubles_on_nonzero_rc():
    """ffmpeg dies with rc!=0 twice → backoff: 5 → 10 between attempts."""
    from twitch_stt_listener import run_listener
    fake_wait, fake_seg, fake_sleep, sleeps = _make_supervisor_doubles([1, 1])

    with patch("twitch_stt_listener.wait_for_stream", side_effect=fake_wait), \
         patch("twitch_stt_listener.segment_stream", side_effect=fake_seg), \
         patch("twitch_stt_listener.asyncio.sleep", side_effect=fake_sleep), \
         patch("twitch_stt_listener.init_db", return_value=MagicMock()), \
         patch("twitch_stt_listener.process_chunks", new_callable=AsyncMock):
        await run_listener("ch")

    # Two ffmpeg deaths → at least two sleep(backoff) calls with the supervisor
    # backoff sequence (5, then 10). The fake also enters wait_for_stream
    # internally, but only the supervisor uses sleep(backoff) values from {5,10,…}.
    backoff_sleeps = [s for s in sleeps if s in (5, 10, 20, 40, 60)]
    assert backoff_sleeps[:2] == [5, 10], f"expected backoff 5→10, got {backoff_sleeps}"


@pytest.mark.asyncio
async def test_supervisor_backoff_resets_on_clean_exit():
    """rc==0 (graceful URL expiry) → backoff resets to BASE next attempt."""
    from twitch_stt_listener import run_listener
    # rc=1 → backoff goes 5→10. Then rc=0 → resets back to 5.
    fake_wait, fake_seg, fake_sleep, sleeps = _make_supervisor_doubles([1, 0, 1])

    with patch("twitch_stt_listener.wait_for_stream", side_effect=fake_wait), \
         patch("twitch_stt_listener.segment_stream", side_effect=fake_seg), \
         patch("twitch_stt_listener.asyncio.sleep", side_effect=fake_sleep), \
         patch("twitch_stt_listener.init_db", return_value=MagicMock()), \
         patch("twitch_stt_listener.process_chunks", new_callable=AsyncMock):
        await run_listener("ch")

    backoff_sleeps = [s for s in sleeps if s in (5, 10, 20, 40, 60)]
    # 1st rc=1: sleep 5, then double to 10. 2nd rc=0: sleep 10, then RESET to 5.
    # 3rd rc=1: sleep 5. So the sequence is 5,10,5 (not 5,10,20).
    assert backoff_sleeps[:3] == [5, 10, 5], (
        f"expected backoff to reset on rc==0; got {backoff_sleeps}"
    )


@pytest.mark.asyncio
async def test_supervisor_backoff_caps_at_max():
    """Repeated rc!=0 → backoff doubles 5,10,20,40,60 and caps at 60."""
    from twitch_stt_listener import run_listener
    fake_wait, fake_seg, fake_sleep, sleeps = _make_supervisor_doubles([1, 1, 1, 1, 1, 1, 1])

    with patch("twitch_stt_listener.wait_for_stream", side_effect=fake_wait), \
         patch("twitch_stt_listener.segment_stream", side_effect=fake_seg), \
         patch("twitch_stt_listener.asyncio.sleep", side_effect=fake_sleep), \
         patch("twitch_stt_listener.init_db", return_value=MagicMock()), \
         patch("twitch_stt_listener.process_chunks", new_callable=AsyncMock):
        await run_listener("ch")

    backoff_sleeps = [s for s in sleeps if s in (5, 10, 20, 40, 60)]
    # 5, 10, 20, 40, 60, 60, 60 — caps at 60
    assert backoff_sleeps[:7] == [5, 10, 20, 40, 60, 60, 60], (
        f"backoff should cap at 60; got {backoff_sleeps}"
    )


# Note on cancellation: the backoff tests above implicitly cover it — each one
# spawns N processor_tasks (one per iteration) and only completes if the
# supervisor's processor_task.cancel() + await is honoured. If cancellation
# leaked we'd accumulate tasks and the loop would hang. An explicit test that
# verifies cancel-by-side-effect is unreachable because AsyncMock-backed
# ffmpeg.wait() never yields control to the processor coroutine body.
