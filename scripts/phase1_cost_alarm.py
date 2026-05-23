"""
phase1_cost_alarm.py — Phase 1 M7 paid LLM cost daily rollup + alarm

讀 records/llm_paid_usage.jsonl (既有 PaidUsageGuard 寫的)、breakdown by:
  - 今日總花費（含距離 daily cap 距離）
  - 本月總花費（含距離 monthly cap 距離）
  - per-caller / per-model breakdown
  - top-3 expensive caller

若 daily cost > PHASE1_DAILY_ALARM_USD（design doc 預估上限 $2 → alarm）
或 monthly cost > PHASE1_MONTHLY_ALARM_USD ($50 → alarm)，print ALARM 並 exit code 1
（caller 可在 cron / launchd 配 webhook 推 Discord）。

Run:
  venv_simon/bin/python scripts/phase1_cost_alarm.py
  venv_simon/bin/python scripts/phase1_cost_alarm.py --daily-alarm 1.5
  venv_simon/bin/python scripts/phase1_cost_alarm.py --json   # JSON 輸出給其它 tool 用

Cron example (每小時跑一次):
  0 * * * * cd ~/Documents/Antigravity/Discord-voice-bot && \\
    venv_simon/bin/python scripts/phase1_cost_alarm.py >> data/cost_alarm.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm_paid import PaidUsageGuard, DEFAULT_PAID_LOG


# Design doc estimated upper bounds
PHASE1_DAILY_ALARM_USD = 2.0
PHASE1_MONTHLY_ALARM_USD = 50.0


def analyze(log_path: Path, daily_alarm_usd: float | None = None, monthly_alarm_usd: float | None = None) -> dict:
    """讀 jsonl、回 {today, month, top_callers, top_models, alarm_today, alarm_month}."""
    if daily_alarm_usd is None:
        daily_alarm_usd = PHASE1_DAILY_ALARM_USD
    if monthly_alarm_usd is None:
        monthly_alarm_usd = PHASE1_MONTHLY_ALARM_USD
    guard = PaidUsageGuard(log_path=log_path)
    today_total = guard.spent_today()
    month_total = guard.spent_month()

    # Per-caller / per-model breakdown
    per_caller_today: dict[str, float] = defaultdict(float)
    per_caller_month: dict[str, float] = defaultdict(float)
    per_model_today: dict[str, float] = defaultdict(float)
    per_model_month: dict[str, float] = defaultdict(float)
    today_count = 0
    month_count = 0

    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()

    rows = guard._rows()
    for r in rows:
        ts = float(r.get("ts", 0) or 0)
        usd = float(r.get("est_usd", 0) or 0)
        caller = r.get("caller", "<unknown>")
        model = r.get("model", "<unknown>")
        if ts >= month_start:
            per_caller_month[caller] += usd
            per_model_month[model] += usd
            month_count += 1
        if ts >= today_start:
            per_caller_today[caller] += usd
            per_model_today[model] += usd
            today_count += 1

    return {
        "log_path": str(log_path),
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
        "today": {
            "total_usd": round(today_total, 4),
            "calls": today_count,
            "alarm_threshold_usd": daily_alarm_usd,
            "alarm": today_total > daily_alarm_usd,
            "per_caller": {k: round(v, 4) for k, v in
                          sorted(per_caller_today.items(), key=lambda x: -x[1])},
            "per_model": {k: round(v, 4) for k, v in
                         sorted(per_model_today.items(), key=lambda x: -x[1])},
        },
        "month": {
            "total_usd": round(month_total, 4),
            "calls": month_count,
            "alarm_threshold_usd": monthly_alarm_usd,
            "alarm": month_total > monthly_alarm_usd,
            "per_caller": {k: round(v, 4) for k, v in
                          sorted(per_caller_month.items(), key=lambda x: -x[1])[:10]},
            "per_model": {k: round(v, 4) for k, v in
                         sorted(per_model_month.items(), key=lambda x: -x[1])},
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Phase 1 paid LLM cost alarm")
    parser.add_argument("--log", type=str, default=str(DEFAULT_PAID_LOG))
    parser.add_argument("--daily-alarm", type=float, default=PHASE1_DAILY_ALARM_USD)
    parser.add_argument("--monthly-alarm", type=float, default=PHASE1_MONTHLY_ALARM_USD)
    parser.add_argument("--json", action="store_true", help="輸出 JSON 給其它 tool 用")
    args = parser.parse_args()

    log_path = Path(args.log)
    if not log_path.exists():
        if args.json:
            print(json.dumps({"error": f"log not found: {log_path}"}))
        else:
            print(f"⚠️  paid log 不存在: {log_path}")
            print(f"   (新 install / 還沒跑過 paid LLM 是正常的)")
        sys.exit(0)

    result = analyze(log_path, daily_alarm_usd=args.daily_alarm, monthly_alarm_usd=args.monthly_alarm)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(1 if result["today"]["alarm"] or result["month"]["alarm"] else 0)

    # Human-readable
    today = result["today"]
    month = result["month"]
    print("=" * 60)
    print(f"PHASE 1 PAID LLM COST ALARM — {result['snapshot_at']}")
    print(f"Log: {result['log_path']}")
    print("=" * 60)
    print()
    print(f"今日 (UTC date): ${today['total_usd']:.4f} / ${args.daily_alarm:.2f} alarm "
          f"({today['calls']} calls)")
    if today["alarm"]:
        print(f"  🚨 ALARM: 超過 daily threshold ${args.daily_alarm:.2f}")
    else:
        used_pct = (today['total_usd'] / args.daily_alarm * 100) if args.daily_alarm > 0 else 0
        print(f"  ✓ 在 threshold 內 ({used_pct:.1f}%)")
    if today["per_caller"]:
        print(f"  Top callers:")
        for caller, usd in list(today["per_caller"].items())[:5]:
            print(f"    {caller:30s} ${usd:.4f}")
    if today["per_model"]:
        print(f"  By model:")
        for model, usd in today["per_model"].items():
            print(f"    {model:30s} ${usd:.4f}")

    print()
    print(f"本月: ${month['total_usd']:.4f} / ${args.monthly_alarm:.2f} alarm "
          f"({month['calls']} calls)")
    if month["alarm"]:
        print(f"  🚨 ALARM: 超過 monthly threshold ${args.monthly_alarm:.2f}")
    else:
        used_pct = (month['total_usd'] / args.monthly_alarm * 100) if args.monthly_alarm > 0 else 0
        print(f"  ✓ 在 threshold 內 ({used_pct:.1f}%)")
    if month["per_caller"]:
        print(f"  Top callers (this month, top 10):")
        for caller, usd in list(month["per_caller"].items())[:10]:
            print(f"    {caller:30s} ${usd:.4f}")

    print("=" * 60)

    sys.exit(1 if today["alarm"] or month["alarm"] else 0)


if __name__ == "__main__":
    main()
