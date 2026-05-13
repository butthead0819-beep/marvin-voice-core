"""
analyze_daily_log.py — --date arg and macOS notification tests.

Rules:
  1. --date YYYY-MM-DD overrides find_latest_slice() to pick that day's file
  2. --date for a non-existent file falls back gracefully (returns None)
  3. notify_discord_review() calls osascript with title containing date + score
  4. notify_discord_review() does not crash on osascript failure
  5. notify_discord_review() on success=False sends failure title
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock


# ── helpers ──────────────────────────────────────────────────────────────────

def _import_module():
    mod_name = "scripts.analyze_daily_log"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    base = Path(__file__).parent.parent
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    return importlib.import_module(mod_name)


# ── 1. --date selects correct slice ─────────────────────────────────────────

def test_date_arg_selects_existing_slice(tmp_path):
    """--date 2026-05-10 → returns 2026-05-10.log when the file exists."""
    log_dir = tmp_path / "records" / "daily"
    log_dir.mkdir(parents=True)
    target = log_dir / "2026-05-10.log"
    target.write_text("=== STT LOG (2026-05-09 12:00 ~ 2026-05-10 12:00) ===\nline1\n", encoding="utf-8")

    mod = _import_module()
    with patch.object(mod, "LOG_DIR", log_dir):
        result = mod.find_slice_for_date("2026-05-10")

    assert result == target


def test_date_arg_returns_none_for_missing_slice(tmp_path):
    """--date for a date with no file → returns None instead of crashing."""
    log_dir = tmp_path / "records" / "daily"
    log_dir.mkdir(parents=True)

    mod = _import_module()
    with patch.object(mod, "LOG_DIR", log_dir):
        result = mod.find_slice_for_date("2026-05-01")

    assert result is None


def test_date_arg_prefers_exact_match_over_latest(tmp_path):
    """--date 2026-05-08 must return that file even when 2026-05-12 is newer."""
    log_dir = tmp_path / "records" / "daily"
    log_dir.mkdir(parents=True)
    for d in ("2026-05-08", "2026-05-09", "2026-05-12"):
        f = log_dir / f"{d}.log"
        f.write_text(f"header for {d}\n" + "x\n" * 5, encoding="utf-8")

    mod = _import_module()
    with patch.object(mod, "LOG_DIR", log_dir):
        result = mod.find_slice_for_date("2026-05-08")

    assert result is not None and result.stem == "2026-05-08"


# ── 2. macOS 通知 ────────────────────────────────────────────────────────────

def test_notify_review_calls_osascript_on_success():
    """成功時應以 osascript 送通知，script 包含日期與分數。"""
    mod = _import_module()
    calls = []

    def _fake_run(cmd, **kw):
        calls.append(cmd)
        return MagicMock(returncode=0)

    with patch("subprocess.run", _fake_run):
        mod.notify_discord_review(
            date="2026-05-12",
            score=7.5,
            trend="改善",
            problem_patterns=[{"pattern": "長等待", "frequency": 3}],
            success=True,
        )

    assert len(calls) == 1
    script_str = " ".join(calls[0])
    assert "osascript" in script_str
    assert "2026-05-12" in script_str
    assert "7.5" in script_str


def test_notify_review_no_crash_on_osascript_error():
    """osascript 失敗（例如非 macOS 環境）不應拋例外。"""
    mod = _import_module()

    def _raise(*a, **kw):
        raise FileNotFoundError("osascript not found")

    with patch("subprocess.run", _raise):
        mod.notify_discord_review(
            date="2026-05-12",
            score=7.5,
            trend="改善",
            problem_patterns=[],
            success=True,
        )  # must not raise


def test_notify_review_failure_alert_contains_error_msg():
    """success=False 時 script 應包含失敗訊息。"""
    mod = _import_module()
    calls = []

    def _fake_run(cmd, **kw):
        calls.append(cmd)
        return MagicMock(returncode=0)

    with patch("subprocess.run", _fake_run):
        mod.notify_discord_review(
            date="2026-05-12",
            score=None,
            trend=None,
            problem_patterns=[],
            success=False,
            error_msg="Gemini API 失敗",
        )

    assert len(calls) == 1
    script_str = " ".join(calls[0])
    assert "Gemini API" in script_str or "失敗" in script_str


def test_notify_review_success_no_problem_still_works():
    """problem_patterns 為空時不崩潰。"""
    mod = _import_module()

    with patch("subprocess.run", MagicMock(return_value=MagicMock(returncode=0))):
        mod.notify_discord_review(
            date="2026-05-12",
            score=8.0,
            trend="持平",
            problem_patterns=[],
            success=True,
        )  # must not raise
