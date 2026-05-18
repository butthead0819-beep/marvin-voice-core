"""
Tests for the memory pressure guard.

Context: 5/18 20:28 incident — macOS returned EDEADLK from file reads
(importlib, posix_spawn) under heavy swap (free pages 0.74%). When
memory is critical, non-essential subsystems (vector store upserts,
ambient features) should skip writes to reduce I/O pressure.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

import memory_guard


@pytest.fixture(autouse=True)
def _reset_cache():
    memory_guard.reset_cache()
    yield
    memory_guard.reset_cache()


def test_returns_true_when_memory_above_threshold():
    with patch("memory_guard.psutil") as mock_psutil:
        mock_psutil.virtual_memory.return_value.percent = 95.0
        assert memory_guard.is_memory_critical(threshold_pct=92.0) is True


def test_returns_false_when_memory_below_threshold():
    with patch("memory_guard.psutil") as mock_psutil:
        mock_psutil.virtual_memory.return_value.percent = 60.0
        assert memory_guard.is_memory_critical(threshold_pct=92.0) is False


def test_returns_false_when_psutil_unavailable():
    with patch("memory_guard.psutil", None):
        assert memory_guard.is_memory_critical(threshold_pct=92.0) is False


def test_returns_false_when_psutil_raises():
    with patch("memory_guard.psutil") as mock_psutil:
        mock_psutil.virtual_memory.side_effect = RuntimeError("vm_stat broken")
        assert memory_guard.is_memory_critical(threshold_pct=92.0) is False


def test_caches_result_within_ttl():
    with patch("memory_guard.psutil") as mock_psutil:
        mock_psutil.virtual_memory.return_value.percent = 95.0
        memory_guard.is_memory_critical(threshold_pct=92.0)
        memory_guard.is_memory_critical(threshold_pct=92.0)
        memory_guard.is_memory_critical(threshold_pct=92.0)
        assert mock_psutil.virtual_memory.call_count == 1


def test_threshold_overridable_via_env(monkeypatch):
    monkeypatch.setenv("MEMORY_GUARD_THRESHOLD_PCT", "50.0")
    with patch("memory_guard.psutil") as mock_psutil:
        mock_psutil.virtual_memory.return_value.percent = 60.0
        assert memory_guard.is_memory_critical() is True


def test_threshold_default_is_92():
    with patch("memory_guard.psutil") as mock_psutil:
        mock_psutil.virtual_memory.return_value.percent = 91.0
        assert memory_guard.is_memory_critical() is False
        memory_guard.reset_cache()
        mock_psutil.virtual_memory.return_value.percent = 93.0
        assert memory_guard.is_memory_critical() is True
