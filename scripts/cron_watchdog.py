"""每日任務看門狗：檢查每個 cron log 的新鮮度 + 失敗標記，壞了回報（可選 Discord 告警）。

排程器（launchd）本身會準時觸發，真正的盲點是「下游 LLM 失敗了卻沒人知道、幾週後才發現」。
這支本機跑（雲端 Claude 讀不到本機 log），每天檢查 → 有問題就告警。

用法：
  python scripts/cron_watchdog.py              # 印出問題，有問題 exit 1
  MARVIN_WATCHDOG_WEBHOOK=<discord_url> python scripts/cron_watchdog.py   # 額外貼 Discord
"""
from __future__ import annotations

import os
import sys
import time

# 總失敗標記（明確的整體失敗，不抓子步驟的 ❌ 以免誤報）
FAIL_MARKERS = ("all 3 attempts failed", "all attempts failed",
                "Traceback (most recent call last)")

_LOG_DIR = os.path.expanduser("~/Library/Logs/Marvin")

# 每個任務 → log + 容許多久沒更新（依排程：每日 36h、每週 8 天）
CHECKS = [
    {"name": "dailyslice",      "log": f"{_LOG_DIR}/slice_cron.log",          "max_age_h": 36},
    {"name": "dailyreview",     "log": f"{_LOG_DIR}/review_cron.log",         "max_age_h": 36},
    {"name": "feedbackbatch",   "log": f"{_LOG_DIR}/feedback_batch_cron.log", "max_age_h": 36},
    {"name": "tasteprofile",    "log": f"{_LOG_DIR}/taste_profile_cron.log",  "max_age_h": 36},
    {"name": "speechdna",       "log": f"{_LOG_DIR}/speechdna_cron.log",      "max_age_h": 192},
    {"name": "tastefingerprint", "log": f"{_LOG_DIR}/taste_fingerprint_cron.log", "max_age_h": 192},
]


def check_cron_health(checks, now_ts, tail_lines: int = 40) -> list[str]:
    """回問題清單（空 = 全健康）。問題：log 不存在 / 太舊沒更新 / 含整體失敗標記。"""
    problems: list[str] = []
    for c in checks:
        name, path, max_age_h = c["name"], c["log"], c["max_age_h"]
        if not os.path.exists(path):
            problems.append(f"{name}：log 不存在（沒跑過？）")
            continue
        age_h = (now_ts - os.path.getmtime(path)) / 3600
        if age_h > max_age_h:
            problems.append(f"{name}：{age_h:.0f}h 沒更新（>{max_age_h}h，可能沒跑）")
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                tail = "".join(f.readlines()[-tail_lines:])
        except Exception:
            continue
        if any(m in tail for m in FAIL_MARKERS):
            problems.append(f"{name}：log 有整體失敗標記")
    return problems


def _notify_discord(webhook: str, problems: list[str]) -> None:
    import json
    import urllib.request
    body = "🚨 Marvin 每日任務看門狗\n" + "\n".join(f"• {p}" for p in problems)
    req = urllib.request.Request(
        webhook, data=json.dumps({"content": body}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[watchdog] Discord 告警失敗：{e}", file=sys.stderr)


def main() -> int:
    problems = check_cron_health(CHECKS, now_ts=time.time())
    if not problems:
        print("[watchdog] ✅ 每日任務全健康")
        return 0
    print("[watchdog] ⚠ 發現問題：")
    for p in problems:
        print(f"  • {p}")
    webhook = os.environ.get("MARVIN_WATCHDOG_WEBHOOK")
    if webhook:
        _notify_discord(webhook, problems)
    return 1


if __name__ == "__main__":
    sys.exit(main())
