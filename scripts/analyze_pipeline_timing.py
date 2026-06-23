#!/usr/bin/env python3
"""分析 records/pipeline_timing.jsonl —— queue_wait vs cleaner 哪個主導 e2e 延遲。

2026-06-22：pipeline_timing.emit() 開始 durable 落盤後，這支把每筆的階段絕對值
（ms-from-endpoint）拆成 delta 段，回報誰是 queue_wait 的主導因，給「砍 cleaner
vs 修 per-speaker 閒置阻塞」的決策用。

段定義（stages 是各階段 ms-from-endpoint 絕對值）：
  queue_wait    = dequeued      − stt_done      （worker 串行排隊 + 裸喚醒閒置等待前段）
  question_wait = question_done − dequeued      （等補問句：裸喚醒 evt.wait 最多 10s）
  cleaner_pure  = cleaner_done  − question_done （真 cleaner LLM ~2.5s 封頂）

用法：python3 scripts/analyze_pipeline_timing.py [--days N] [--min-samples N]
無資料/樣本不足時明確說「再等」，不硬擠結論。
"""
from __future__ import annotations

import argparse
import datetime
import json
import statistics
from pathlib import Path

INPUT = Path("records/pipeline_timing.jsonl")
# pipeline_timing 只在 runtime 寫（guard 擋 pytest），但保險起見仍濾測試 fixture
_TEST_SPEAKERS = {"Alice", "Bob", "Carol", "Dave", "test", "TestUser", "tester"}


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _seg(stages: dict, a: str, b: str):
    """b − a，兩端都在才回；缺一回 None（該筆該段不計）。"""
    if a in stages and b in stages:
        return stages[b] - stages[a]
    return None


def _pct(vals: list[float], q: float) -> float:
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * q))]


def analyze(rows: list[dict], days: int, min_samples: int) -> str:
    if not rows:
        return ("[pipeline_timing] records/pipeline_timing.jsonl 尚無資料。"
                "bot 需跑到 emit() 落盤的新 code 且有真實派發才會累積。")
    now = max(r.get("ts", 0) for r in rows)
    cut = now - days * 86400
    win = [r for r in rows
           if r.get("ts", 0) >= cut and r.get("speaker") not in _TEST_SPEAKERS]
    n = len(win)
    span_lo = datetime.datetime.fromtimestamp(min(r["ts"] for r in win)).date() if win else "—"
    span_hi = datetime.datetime.fromtimestamp(now).date()

    segs = {"queue_wait": [], "question_wait": [], "cleaner_pure": []}
    totals = []
    for r in win:
        st = r.get("stages") or {}
        qw = _seg(st, "stt_done", "dequeued")
        qq = _seg(st, "dequeued", "question_done")
        cp = _seg(st, "question_done", "cleaner_done")
        if qw is not None:
            segs["queue_wait"].append(qw)
        if qq is not None:
            segs["question_wait"].append(qq)
        if cp is not None:
            segs["cleaner_pure"].append(cp)
        if isinstance(r.get("total_ms"), (int, float)):
            totals.append(r["total_ms"])

    lines = [f"[pipeline_timing] 近 {days}d：{n} 筆真人派發（{span_lo}→{span_hi}）"]

    if n < min_samples:
        lines.append(f"⏳ 樣本不足（{n}/{min_samples}），先別下結論、繼續累積。")
        return "\n".join(lines)

    if totals:
        lines.append(f"  total_ms       p50={_pct(totals,.5):.0f} p90={_pct(totals,.9):.0f}")
    medians = {}
    for name, vals in segs.items():
        if vals:
            medians[name] = statistics.median(vals)
            lines.append(f"  {name:14} n={len(vals):4} "
                         f"p50={_pct(vals,.5):.0f} p90={_pct(vals,.9):.0f} max={max(vals):.0f}")
        else:
            lines.append(f"  {name:14} 無資料（該段未打點）")

    if medians:
        dominant = max(medians, key=medians.get)
        verdict = {
            "queue_wait": "→ worker 串行排隊主導：考慮 per-speaker 序列化。",
            "question_wait": "→ 裸喚醒 evt.wait(10s) 閒置等待主導：把確認等待移出 worker。",
            "cleaner_pure": "→ cleaner LLM 主導：砍 confirmation 第二次清洗（已驗證安全）。",
        }
        lines.append(f"🎯 主導段 = {dominant} (p50={medians[dominant]:.0f}ms) {verdict[dominant]}")
    return "\n".join(lines)


REPORT = Path("records/pipeline_timing_report.md")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--min-samples", type=int, default=30)
    ap.add_argument("--no-report", action="store_true",
                    help="只印 stdout，不寫 records/pipeline_timing_report.md")
    args = ap.parse_args()
    out = analyze(_load(INPUT), args.days, args.min_samples)
    print(out)
    if not args.no_report:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        REPORT.write_text(
            f"# pipeline_timing 延遲分段（每日 cron 自動更新）\n\n"
            f"_最後更新 {stamp}_\n\n```\n{out}\n```\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
