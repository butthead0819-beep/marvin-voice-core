#!/usr/bin/env python3
"""
launchd wrapper for analyze_daily_log.py.

macOS Sequoia+ 禁止 LaunchAgent 直接 spawn /bin/bash（posix_spawn returns EPERM），
所以 plist 改成直接呼叫這個 Python wrapper。本檔負責：

1. 設定 venv 工作目錄與環境變數（取代原 bash 的 export）
2. 用 subprocess 跑 analyze_daily_log.py，失敗時最多重試 3 次
3. 將完整輸出寫進 stdout/stderr（launchd 會 redirect 到 review_cron.log）

EINTR 問題（Python <frozen getpath> 被 launchd signal 中斷）由子 process 啟動
時的 subprocess retry 機制吸收 — 第二次 spawn 通常就 OK 了。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

WORKDIR = Path("/Users/jackhuang/Documents/Antigravity/Discord-voice-bot")
VENV_PY = WORKDIR / "venv_simon" / "bin" / "python3"
SCRIPT = WORKDIR / "scripts" / "analyze_daily_log.py"

ENV_OVERRIDES = {
    "HOME": "/Users/jackhuang",
    "PATH": f"{WORKDIR}/venv_simon/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    "PYTHONNOUSERSITE": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}

MAX_ATTEMPTS = 3
RETRY_SLEEP = 15  # 秒


def main() -> int:
    print(f"[run_daily_review.py] === entered at {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)
    os.chdir(WORKDIR)

    env = os.environ.copy()
    env.update(ENV_OVERRIDES)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"[run_daily_review.py] 🚀 attempt {attempt} at {time.strftime('%H:%M:%S')}", flush=True)
        try:
            r = subprocess.run(
                [str(VENV_PY), str(SCRIPT)],
                cwd=str(WORKDIR),
                env=env,
                check=False,
            )
            if r.returncode == 0:
                print(f"[run_daily_review.py] ✅ success at {time.strftime('%H:%M:%S')}", flush=True)
                return 0
            print(f"[run_daily_review.py] ⚠ attempt {attempt} exited {r.returncode}", flush=True)
        except Exception as e:
            print(f"[run_daily_review.py] ⚠ attempt {attempt} raised: {e}", flush=True)

        if attempt < MAX_ATTEMPTS:
            print(f"[run_daily_review.py] sleeping {RETRY_SLEEP}s before retry...", flush=True)
            time.sleep(RETRY_SLEEP)

    print(f"[run_daily_review.py] ❌ all {MAX_ATTEMPTS} attempts failed", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
