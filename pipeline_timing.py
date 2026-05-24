"""Voice pipeline stage timing — measure VAD→STT→Cleaner→Intent latency.

ContextVar-based: stages don't pass timing through function signatures.
`asyncio.create_task` copies the current context (Python 3.7+ guarantee),
so once `start()` is called inside an async frame, `mark()` / `emit()` from
downstream awaits and tasks see the same dict.

Note: `loop.call_soon_threadsafe` (used by sink to bridge thread → async)
does NOT propagate context. So `start()` must be called INSIDE the async
entry (process_audio_slice), not in the sync sink thread.

Output line shape:
  [STAGE_TIMING] speaker=狗與露 sttstart=12ms sttdone=487ms cleanerdone=1203ms intentdispatched=1208ms total=1208ms text='播放周杰倫的稻香'

Grep + awk friendly: `grep STAGE_TIMING bot_stdout.log | awk ...`
"""
from __future__ import annotations

import contextvars
import time

_STAGES = ("stt_start", "stt_done", "cleaner_done", "intent_dispatched")

_timing: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "pipeline_timing", default=None
)


def start() -> dict:
    """Begin a new timing record at the current async frame. Idempotent per task."""
    d: dict = {"endpoint": time.monotonic()}
    _timing.set(d)
    return d


def mark(stage: str) -> None:
    """Record a stage timestamp; no-op if no timing context started."""
    d = _timing.get()
    if d is not None:
        d[stage] = time.monotonic()


def emit(speaker: str, text: str, suffix: str = "") -> None:
    """Print one [STAGE_TIMING] line. Silent if no timing context started."""
    d = _timing.get()
    if d is None or "endpoint" not in d:
        return
    ep = d["endpoint"]
    parts = []
    for s in _STAGES:
        if s in d:
            tag = s.replace("_", "")
            parts.append(f"{tag}={(d[s] - ep) * 1000:.0f}ms")
    total_end = d.get("intent_dispatched", time.monotonic())
    total_ms = (total_end - ep) * 1000
    snippet = (text or "")[:40]
    print(
        f"[STAGE_TIMING] speaker={speaker} {' '.join(parts)} "
        f"total={total_ms:.0f}ms text={snippet!r}{suffix}",
        flush=True,
    )


def snapshot() -> dict | None:
    """Read-only access to current timing dict (for tests)."""
    return _timing.get()
