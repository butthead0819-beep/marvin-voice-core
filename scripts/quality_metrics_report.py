"""每日 Marvin 品質指標聚合報告（per feedback_marvin_quality_metrics）。

讀 records/quality_metrics.jsonl 的當日 rows，聚合成 rate/p50/p95，寫一份 markdown。
掛 com.antigravity.marvin.dailyreview cron（與 daily review 並列）。

Phase 1：false-responding（Track-B wake proxy）+ react_ms placeholder。
Phase 2+ 把 react_ms / interruption / recall section 接上（capture 點補齊後自動有資料）。
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from quality_metrics import (  # noqa: E402
    DEFAULT_METRICS_LOG, read_metrics,
    summarize_false_responding, summarize_latency, summarize_interruption,
    summarize_recall,
)


def day_bounds(date_str: str | None = None) -> tuple[float, float, str]:
    """當地日界 [start, end) 的 ts + 標籤。date_str 缺 → 今天。"""
    d = datetime.strptime(date_str, "%Y-%m-%d") if date_str else datetime.now()
    start = d.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.timestamp(), end.timestamp(), start.strftime("%Y-%m-%d")


def build_report(rows: list[dict], date_label: str) -> str:
    fr = summarize_false_responding(rows)
    react = summarize_latency([r for r in rows if r.get("metric") == "react"], "react_ms")
    lines = [f"# Marvin 品質指標 — {date_label}", ""]

    lines.append("## False responding（Track-B wake proxy：empty harvest = 誤喚醒）")
    if fr["total"]:
        lines.append(f"- wakes: {fr['total']}　false: {fr['false']}")
        lines.append(f"- **false rate: {fr['false_rate'] * 100:.1f}%**")
    else:
        lines.append("- （今日無 Track-B wake 樣本）")
    lines.append("")

    lines.append("## Time to react")
    if react["count"]:
        lines.append(f"- count: {react['count']}")
        lines.append(f"- **p50: {react['p50']}ms　p95: {react['p95']}ms　mean: {react['mean']}ms**")
    else:
        lines.append("- （今日無 react 樣本）")
    lines.append("")

    lines.append("## Bad-timing interruption（Marvin 開口瞬間有人類正在說話）")
    it_all = summarize_interruption(rows)
    it_idle = summarize_interruption(rows, idle_only=True)
    if it_all["total"]:
        lines.append(f"- 開口次數: {it_all['total']}　打斷: {it_all['interrupted']}")
        lines.append(f"- **打斷率: {it_all['interrupt_rate'] * 100:.1f}%**"
                     f"（排除回聲嫌疑 idle-only: {it_idle['interrupt_rate'] * 100:.1f}%, n={it_idle['total']}）")
    else:
        lines.append("- （今日無 TTS 開口樣本）")
    lines.append("")

    lines.append("## Recall（weekly active probe）")
    rc = summarize_recall(rows)
    if rc["total"]:
        lines.append(f"- cases: {rc['total']}　correct: {rc['correct']}")
        lines.append(f"- **recall accuracy: {rc['accuracy'] * 100:.1f}%**")
    else:
        lines.append("- （本期無 probe 樣本 — 填 recall_probe_cases.json 真實 ground truth 後每週跑）")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD（缺=今天）")
    ap.add_argument("--log", default=str(DEFAULT_METRICS_LOG))
    ap.add_argument("--out-dir", default="records")
    args = ap.parse_args()

    since, until, label = day_bounds(args.date)
    rows = read_metrics(Path(args.log), since_ts=since, until_ts=until)
    report = build_report(rows, label)

    out = Path(args.out_dir) / f"quality_metrics_{label}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n→ {out}")


if __name__ == "__main__":
    main()
