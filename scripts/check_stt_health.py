#!/usr/bin/env python3
"""Marvin STT heartbeat 健康檢查

讀 HEARTBEAT_PATH（預設 /tmp/marvin_stt_heartbeat）的 mtime，
過期 → exit 1；找不到 → exit 2；新鮮 → exit 0。

Env vars:
  MARVIN_STT_HEARTBEAT      heartbeat 檔路徑（預設 /tmp/marvin_stt_heartbeat）
  MARVIN_STT_STALE_SECONDS  過期門檻（預設 300 秒 = 5 分鐘）

Usage:
  python scripts/check_stt_health.py
  → cron 每 5 分鐘跑一次，exit 1 時可串接 Discord webhook 通知
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def main() -> int:
    hb_path = Path(os.environ.get("MARVIN_STT_HEARTBEAT", "/tmp/marvin_stt_heartbeat"))
    stale_secs = int(os.environ.get("MARVIN_STT_STALE_SECONDS", "300"))

    if not hb_path.exists():
        print(f"[STT health] missing heartbeat file: {hb_path}", file=sys.stderr)
        return 2

    age = time.time() - hb_path.stat().st_mtime
    if age > stale_secs:
        mins = age / 60
        print(f"[STT health] STALE — heartbeat is {mins:.1f}m old "
              f"(threshold {stale_secs}s) — STT possibly stuck", file=sys.stderr)
        return 1

    print(f"[STT health] OK — heartbeat {age:.0f}s ago")
    return 0


if __name__ == "__main__":
    sys.exit(main())
