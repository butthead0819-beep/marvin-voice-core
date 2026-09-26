"""Summon 觸發 daily review 的 once-per-day guard——防同日多次 summon 重跑、重複付費。

設計（2026-07-08 使用者）：launchd 日排程脆弱(07-06 後靜默停 fire)→改當天第一次 summon
背景跑 review(不擋登場、成敗印 bot log)。guard＝完成標記 quality_metrics_<today>.md 存在
就跳過(launchd 或先前 summon 已跑)。付費記帳沿用 call_paid_review(cwd 對→寫同一帳本)。
"""
from cogs.voice_controller_connection import ConnectionMixin

_done = ConnectionMixin._daily_review_done_today


def test_done_when_marker_exists(tmp_path):
    (tmp_path / "quality_metrics_2026-07-08.md").write_text("x")
    assert _done("2026-07-08", str(tmp_path)) is True


def test_not_done_when_marker_absent(tmp_path):
    assert _done("2026-07-08", str(tmp_path)) is False


def test_not_done_when_only_other_date(tmp_path):
    (tmp_path / "quality_metrics_2026-07-07.md").write_text("x")
    assert _done("2026-07-08", str(tmp_path)) is False


# ── 巡邏迴圈兜底（2026-09-26）：9/23 後只有 AutoRejoin 靜默回台、沒人手動 summon，
# launchd 備援又只排週一 → 連 3 天沒跑。改由 slow_system_loop 每 tick 檢查，12 點後才觸發。
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs.voice_controller_system_loops import SystemLoopsMixin


def _loop_self(running=False):
    s = SimpleNamespace(_daily_review_running=running)
    s._maybe_run_daily_review = AsyncMock()
    return s


@pytest.mark.asyncio
async def test_loop_kicks_review_after_noon():
    s = _loop_self()
    SystemLoopsMixin._maybe_kick_daily_review(s, now_hour=12)
    await asyncio.sleep(0)
    s._maybe_run_daily_review.assert_awaited_once()


@pytest.mark.asyncio
async def test_loop_does_not_kick_before_noon():
    s = _loop_self()
    SystemLoopsMixin._maybe_kick_daily_review(s, now_hour=11)
    await asyncio.sleep(0)
    s._maybe_run_daily_review.assert_not_awaited()


@pytest.mark.asyncio
async def test_loop_does_not_kick_while_review_running():
    s = _loop_self(running=True)
    SystemLoopsMixin._maybe_kick_daily_review(s, now_hour=15)
    await asyncio.sleep(0)
    s._maybe_run_daily_review.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_daily_review_sets_running_flag_and_clears_it(monkeypatch):
    """review 跑好幾分鐘，期間下一個 10 分鐘 tick 不能再觸發第二次（防重複付費）。"""
    seen = []
    s = SimpleNamespace(_daily_review_running=False)
    s._daily_review_done_today = lambda today: False
    s._run_daily_review_scripts = lambda: ConnectionMixin._run_daily_review_scripts(s)

    async def fake_exec(*a, **k):
        seen.append(s._daily_review_running)
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await ConnectionMixin._maybe_run_daily_review(s)
    assert seen and all(seen), "跑 script 期間旗標要是 True"
    assert s._daily_review_running is False


@pytest.mark.asyncio
async def test_run_daily_review_skips_when_already_running(monkeypatch):
    s = SimpleNamespace(_daily_review_running=True)
    s._daily_review_done_today = lambda today: False
    s._run_daily_review_scripts = lambda: ConnectionMixin._run_daily_review_scripts(s)
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await ConnectionMixin._maybe_run_daily_review(s)
    spawn.assert_not_awaited()
