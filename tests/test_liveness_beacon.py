"""TDD: 防線① in-bot 心跳信標 — 證明 event loop 活著。

失效模式（2026-06-29 busy-spin 事故）：進程活著但 event loop 凍住，
launchd 不會重啟、ErrorDispatcher 也發不出 DM。信標由 event loop 內
的 task 定期落盤 → 外部 probe 驗 staleness 才能抓到「凍住」。
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from liveness_beacon import write_beacon, run_beacon


def test_write_beacon_writes_ts_and_extra(tmp_path):
    p = tmp_path / "heartbeat.json"
    write_beacon(p, extra={"queue_depth": 3})
    data = json.loads(p.read_text())
    assert abs(data["ts"] - time.time()) < 5
    assert data["queue_depth"] == 3


def test_write_beacon_never_raises_on_io_error():
    """鐵則：instrument 不得炸熱路徑——路徑不可寫也要靜默。"""
    write_beacon("/nonexistent-dir/x/heartbeat.json", extra={})   # 不 raise 即過


@pytest.mark.asyncio
async def test_run_beacon_writes_periodically_and_stops(tmp_path):
    p = tmp_path / "heartbeat.json"
    stop = asyncio.Event()
    task = asyncio.create_task(run_beacon(p, interval_s=0.05, stop_event=stop))
    await asyncio.sleep(0.12)   # 至少寫 2 次
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    data = json.loads(p.read_text())
    assert abs(data["ts"] - time.time()) < 5


@pytest.mark.asyncio
async def test_run_beacon_yields_event_loop(tmp_path):
    """信標自己不得 busy-spin（不然防線本身變事故源）。"""
    p = tmp_path / "heartbeat.json"
    stop = asyncio.Event()
    task = asyncio.create_task(run_beacon(p, interval_s=0.05, stop_event=stop))
    # 若 busy-spin，這個 sleep 會被餓死拖很久
    t0 = time.monotonic()
    await asyncio.sleep(0.05)
    assert time.monotonic() - t0 < 0.5
    stop.set()
    await asyncio.wait_for(task, timeout=1)
