"""Memory pressure guard.

When system memory usage crosses a critical threshold, callers can use
``is_memory_critical()`` to skip non-essential work (e.g. vector store
writes, ambient features) — the goal is to avoid pushing macOS into the
state where the kernel returns EDEADLK from file I/O (read(2),
posix_spawn(2)) due to page-cache / swap / fcntl lock contention.

The 5/18 20:28 incident: macOS returned EDEADLK on every Swift STT
subprocess call after the bot accumulated heavy swap (free pages 0.74%),
silencing the wake pipeline for 4 minutes.

Threshold can be tuned via ``MEMORY_GUARD_THRESHOLD_PCT`` env var
(default 92.0). Result is cached for 5s to keep the check cheap in hot
paths.
"""
from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger("MemoryGuard")

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore[assignment]

_CACHE_TTL = 5.0
_DEFAULT_THRESHOLD_PCT = 92.0

_last_check_ts = 0.0
_last_result = False


def is_memory_critical(threshold_pct: float | None = None) -> bool:
    """Return True if RAM usage% >= threshold. Cached for ``_CACHE_TTL``s."""
    global _last_check_ts, _last_result
    now = time.monotonic()
    if now - _last_check_ts < _CACHE_TTL:
        return _last_result

    if threshold_pct is None:
        try:
            threshold_pct = float(
                os.getenv("MEMORY_GUARD_THRESHOLD_PCT", _DEFAULT_THRESHOLD_PCT)
            )
        except ValueError:
            threshold_pct = _DEFAULT_THRESHOLD_PCT

    if psutil is None:
        _last_result = False
    else:
        try:
            pct = psutil.virtual_memory().percent
            critical = pct >= threshold_pct
            if critical:
                logger.warning(
                    f"⚠️ [MemoryGuard] critical: {pct:.1f}% >= {threshold_pct:.1f}% "
                    f"— skipping non-essential writes"
                )
            _last_result = critical
        except Exception as e:
            logger.warning(f"[MemoryGuard] psutil failed: {e} — assume not critical")
            _last_result = False

    _last_check_ts = now
    return _last_result


def reset_cache() -> None:
    """Clear the TTL cache. Use only in tests."""
    global _last_check_ts, _last_result
    _last_check_ts = 0.0
    _last_result = False
