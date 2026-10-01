"""
scripts/paid_cost_report.py — 付費成本報表工具（唯讀）。

讀取 records/llm_paid_usage.jsonl，統計：
- 每月總 est_usd、呼叫數、tokens
- 當月每天的 est_usd（依 Asia/Taipei 時區切日）
- 當月依 caller 排序的前 10 名
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TAIPEI_TZ = ZoneInfo("Asia/Taipei")
DEFAULT_PATH = Path("records/llm_paid_usage.jsonl")


def generate_report(path: Path | str, month: str | None = None) -> dict:
    path = Path(path)
    if not month:
        month = datetime.now(TAIPEI_TZ).strftime("%Y-%m")

    total_est_usd = 0.0
    total_calls = 0
    total_tokens = 0
    daily_usd: dict[str, float] = defaultdict(float)
    caller_stats: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"est_usd": 0.0, "calls": 0, "tokens": 0}
    )

    if not path.exists():
        return {
            "month": month,
            "total_est_usd": 0.0,
            "total_calls": 0,
            "total_tokens": 0,
            "daily_usd": {},
            "top_callers": [],
        }

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts = rec.get("ts")
            if not ts:
                continue

            dt = datetime.fromtimestamp(ts, tz=TAIPEI_TZ)
            row_month = dt.strftime("%Y-%m")
            if row_month != month:
                continue

            day_key = dt.strftime("%Y-%m-%d")
            est_usd = float(rec.get("est_usd") or 0.0)
            tokens = int(rec.get("tokens") or 0)
            caller = rec.get("caller") or "unknown"

            total_est_usd += est_usd
            total_calls += 1
            total_tokens += tokens

            daily_usd[day_key] += est_usd
            caller_stats[caller]["est_usd"] += est_usd
            caller_stats[caller]["calls"] += 1
            caller_stats[caller]["tokens"] += tokens

    sorted_callers = sorted(
        [
            {
                "caller": k,
                "est_usd": v["est_usd"],
                "calls": v["calls"],
                "tokens": v["tokens"],
            }
            for k, v in caller_stats.items()
        ],
        key=lambda x: x["est_usd"],
        reverse=True,
    )

    return {
        "month": month,
        "total_est_usd": total_est_usd,
        "total_calls": total_calls,
        "total_tokens": total_tokens,
        "daily_usd": dict(sorted(daily_usd.items())),
        "top_callers": sorted_callers[:10],
    }


def print_report(report: dict) -> None:
    month = report["month"]
    print("=" * 60)
    print(f"💰 付費 LLM 成本月報 — {month} (時區: Asia/Taipei)")
    print("=" * 60)
    print(f"當月總估算費用: ${report['total_est_usd']:.4f} USD")
    print(f"當月總呼叫次數: {report['total_calls']}")
    print(f"當月總 Token 數: {report['total_tokens']:,}")
    print("-" * 60)

    print("📅 每日花費分組:")
    if not report["daily_usd"]:
        print("  (當月無資料)")
    else:
        for day, usd in report["daily_usd"].items():
            print(f"  {day}: ${usd:.4f} USD")

    print("-" * 60)
    print("🏆 Caller 費用排行 (Top 10):")
    if not report["top_callers"]:
        print("  (無資料)")
    else:
        for idx, item in enumerate(report["top_callers"], start=1):
            print(
                f"  {idx:2d}. {item['caller']:<28} "
                f"${item['est_usd']:.4f} USD | "
                f"{item['calls']:4d} calls | "
                f"{item['tokens']:8,d} tokens"
            )
    print("=" * 60)


def main() -> int:
    parser = argparse.ArgumentParser(description="產生 LLM 付費成本報表")
    parser.add_argument("--month", default=None, help="報表月份 (YYYY-MM)，預設為當月")
    parser.add_argument("--path", default=str(DEFAULT_PATH), help="帳本路徑")

    args = parser.parse_args()
    report = generate_report(Path(args.path), month=args.month)
    print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
