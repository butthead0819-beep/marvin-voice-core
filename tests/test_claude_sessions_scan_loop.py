"""
tests/test_claude_sessions_scan_loop.py
TDD：scripts/scan_claude_sessions.py 的 run_claude_sessions_scan_loop 背景迴圈。

比照 car_mode.run_car_ttl_loop 同一套測試風格：注入 sleep_fn/should_stop，
一拍失敗不弄垮迴圈。
"""
import pytest

from scripts.scan_claude_sessions import run_claude_sessions_scan_loop


@pytest.mark.asyncio
async def test_loop_calls_scan_and_save_each_tick(tmp_path):
    calls = []

    def fake_scan(sessions_dir, projects_dir):
        calls.append((sessions_dir, projects_dir))
        return [{"session_id": "abc"}]

    saved = []

    def fake_save(*, sessions, path):
        saved.append((sessions, path))

    ticks = {"n": 0}

    async def fake_sleep(_):
        pass

    def should_stop():
        ticks["n"] += 1
        return ticks["n"] > 3

    await run_claude_sessions_scan_loop(
        sessions_dir="sdir", projects_dir="pdir", state_path="spath",
        scan_fn=fake_scan, save_fn=fake_save, sleep_fn=fake_sleep, should_stop=should_stop)

    assert len(calls) == 3
    assert saved == [([{"session_id": "abc"}], "spath")] * 3


@pytest.mark.asyncio
async def test_loop_swallows_exception_and_keeps_going(tmp_path):
    ticks = {"n": 0}

    def boom(sessions_dir, projects_dir):
        raise RuntimeError("scan failed")

    async def fake_sleep(_):
        pass

    def should_stop():
        ticks["n"] += 1
        return ticks["n"] > 2

    # 不應該炸出例外
    await run_claude_sessions_scan_loop(
        sessions_dir="sdir", projects_dir="pdir", state_path="spath",
        scan_fn=boom, save_fn=lambda **kw: None, sleep_fn=fake_sleep, should_stop=should_stop)
