"""Tests for SukiBudget (SQLite backend)."""
import pytest
from unittest.mock import patch
from datetime import datetime
from suki_budget import SukiBudget


@pytest.fixture
def budget(tmp_path):
    return SukiBudget(db_path=str(tmp_path / "budget.db"), max_tokens=1000)


# ── Basic token tracking ──────────────────────────────────────────────────────

def test_initial_tokens_zero(budget):
    assert budget.tokens == 0
    assert not budget.is_circuit_open()


def test_add_tokens_accumulates(budget):
    budget.add_tokens(300)
    budget.add_tokens(200)
    assert budget.tokens == 500


def test_get_info_returns_correct_percentage(budget):
    budget.add_tokens(500)
    info = budget.get_info()
    assert info["used"] == 500
    assert info["max"] == 1000
    assert info["percentage"] == 50.0


# ── Circuit breaker ───────────────────────────────────────────────────────────

def test_circuit_opens_at_max(budget):
    budget.add_tokens(999)
    assert not budget.is_circuit_open()
    budget.add_tokens(1)
    assert budget.is_circuit_open()


def test_add_tokens_exhausted_flag(budget):
    status = budget.add_tokens(1001)
    assert status["is_exhausted"] is True


# ── Threshold warnings ────────────────────────────────────────────────────────

def test_80_percent_warning_triggers_once(budget):
    status = budget.add_tokens(800)
    assert status["trigger_80"] is True
    status2 = budget.add_tokens(1)
    assert status2["trigger_80"] is False   # should not fire again


def test_95_percent_warning_triggers_once(budget):
    status = budget.add_tokens(950)
    assert status["trigger_95"] is True
    status2 = budget.add_tokens(1)
    assert status2["trigger_95"] is False


def test_no_spurious_warning_below_threshold(budget):
    status = budget.add_tokens(700)
    assert status["trigger_80"] is False
    assert status["trigger_95"] is False


# ── Daily reset ───────────────────────────────────────────────────────────────

def test_daily_reset_zeroes_tokens(tmp_path):
    db = str(tmp_path / "budget.db")

    with patch("suki_budget.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2025, 1, 1)
        b = SukiBudget(db_path=db, max_tokens=1000)
        b.add_tokens(500)
        assert b.tokens == 500

    # New day
    with patch("suki_budget.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2025, 1, 2)
        b2 = SukiBudget(db_path=db, max_tokens=1000)
        assert b2.tokens == 0
        assert not b2.is_circuit_open()


# ── total_limit property ──────────────────────────────────────────────────────

def test_total_limit_property(budget):
    assert budget.total_limit == 1000
