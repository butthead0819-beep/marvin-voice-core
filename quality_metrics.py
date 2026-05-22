"""Marvin 品質指標 capture + 聚合（per feedback_marvin_quality_metrics）。

四指標：time to react / false responding / bad-timing interruption / remember-recall。
**capture ≠ aggregate**：事件當下打一筆 jsonl（容錯、永不 raise、夠快可在熱路徑
fire-and-forget），每日由 scripts/quality_metrics_report.py 聚合成 rate/p50/p95。

鐵則：instrument 不得拖慢熱路徑——尤其 react time，量測自己若加 IO 阻塞就惡化了
被量的指標。record_metric 同步 append 一行（<1ms，對秒級事件可接受），IO 失敗靜默略過。

Phase 1：管道 + false-responding。latency/percentile 骨架給 Phase 2 react_ms 用。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Optional

DEFAULT_METRICS_LOG = Path("records/quality_metrics.jsonl")


def record_metric(metric: str, *, path: Path = DEFAULT_METRICS_LOG,
                  clock: Callable[[], float] = time.time, **fields) -> None:
    """事件當下 append 一行 {ts, metric, **fields}。永不 raise（IO 失敗只略過）。"""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": clock(), "metric": metric, **fields}
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_metrics(path: Path = DEFAULT_METRICS_LOG, *, metric: Optional[str] = None,
                 since_ts: Optional[float] = None, until_ts: Optional[float] = None) -> list[dict]:
    """讀 jsonl（壞行跳過）。可選 filter metric / 時間窗 [since, until)。"""
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict] = []
    try:
        for line in p.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if metric is not None and r.get("metric") != metric:
                continue
            ts = float(r.get("ts", 0) or 0)
            if since_ts is not None and ts < since_ts:
                continue
            if until_ts is not None and ts >= until_ts:
                continue
            rows.append(r)
    except Exception:
        return rows
    return rows


def percentile(values, pct: float) -> float:
    """線性插值 percentile（pct 0-100）。空 → 0.0。"""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return 0.0
    if len(xs) == 1:
        return float(xs[0])
    k = (len(xs) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    frac = k - lo
    return float(xs[lo] * (1 - frac) + xs[hi] * frac)


def summarize_false_responding(rows: list[dict]) -> dict:
    """rows 內 metric=false_responding（每筆有 was_false bool）→ total/false/false_rate。"""
    fr = [r for r in rows if r.get("metric") == "false_responding"]
    total = len(fr)
    false_n = sum(1 for r in fr if r.get("was_false"))
    return {"total": total, "false": false_n,
            "false_rate": round(false_n / total, 4) if total else 0.0}


def summarize_interruption(rows: list[dict], *, idle_only: bool = False) -> dict:
    """rows 內 metric=interruption（每筆有 interrupted bool + was_playing）→ total/interrupted/rate。

    idle_only：只算 Marvin 開口前沒在播的（was_playing=False），排除「接續回應時 user_is_speaking
    其實是 Marvin 自己回聲」的 echo 嫌疑樣本，得到較乾淨的打斷率。
    """
    iv = [r for r in rows if r.get("metric") == "interruption"]
    if idle_only:
        iv = [r for r in iv if not r.get("was_playing")]
    total = len(iv)
    n = sum(1 for r in iv if r.get("interrupted"))
    return {"total": total, "interrupted": n,
            "interrupt_rate": round(n / total, 4) if total else 0.0}


def summarize_recall(rows: list[dict]) -> dict:
    """rows 內 metric=recall（每筆有 correct bool）→ total/correct/accuracy。

    weekly active probe：對已知 ground truth cases 查 Marvin 記憶、比對 → correct。
    """
    rc = [r for r in rows if r.get("metric") == "recall"]
    total = len(rc)
    correct = sum(1 for r in rc if r.get("correct"))
    return {"total": total, "correct": correct,
            "accuracy": round(correct / total, 4) if total else 0.0}


def summarize_latency(rows: list[dict], field: str = "react_ms") -> dict:
    """通用 latency 聚合（Phase 2 react_ms 用）→ count/p50/p95/mean。"""
    vals = [float(r[field]) for r in rows if r.get(field) is not None]
    if not vals:
        return {"count": 0, "p50": 0.0, "p95": 0.0, "mean": 0.0}
    return {"count": len(vals),
            "p50": round(percentile(vals, 50), 1),
            "p95": round(percentile(vals, 95), 1),
            "mean": round(sum(vals) / len(vals), 1)}
