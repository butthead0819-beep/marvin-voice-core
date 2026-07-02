"""防線① in-bot 心跳信標 — 證明 event loop 活著。

失效模式（2026-06-29 busy-spin 事故）：進程活著但 event loop 凍住——
launchd 不重啟（進程沒死）、ErrorDispatcher 發不出 DM（gateway 也凍）。
唯一可靠的偵測：event loop 內的 task 定期落盤時戳，外部 probe
（scripts/pipeline_heartbeat_probe.py，launchd cron）驗 staleness。

鐵則：instrument 不得炸熱路徑——IO 失敗靜默略過（同 quality_metrics 慣例）。
kill-switch：env MARVIN_HEARTBEAT=0（預設開）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_BEACON_PATH = Path("records/heartbeat.json")
DEFAULT_INTERVAL_S = 30.0


def write_beacon(path: Path | str, extra: dict | None = None) -> None:
    """落盤一筆 {ts, **extra}。永不 raise。"""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": time.time(), **(extra or {})}
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


async def run_beacon(path: Path | str = DEFAULT_BEACON_PATH,
                     interval_s: float = DEFAULT_INTERVAL_S,
                     stop_event: asyncio.Event | None = None,
                     extra_fn=None) -> None:
    """信標迴圈：每 interval_s 落盤一次，直到 stop_event。

    用 wait_for(stop_event.wait(), timeout) 讓出 loop——信標自己
    絕不 busy-spin（防線不能變事故源）。extra_fn（可選、sync、必須便宜）
    回傳附加欄位 dict。
    """
    stop_event = stop_event or asyncio.Event()
    while not stop_event.is_set():
        extra = None
        try:
            extra = extra_fn() if extra_fn is not None else None
        except Exception:
            extra = None
        write_beacon(path, extra)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
    write_beacon(path, {"stopped": True})
