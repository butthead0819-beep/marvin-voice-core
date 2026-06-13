#!/usr/bin/env python3
"""Plan 12 Phase A kill-gate：非喚醒語句價值窗分析。

讀 records/volatile_shadow.jsonl，算「落在串流早切價值窗（audio_ms ≥ min_ms）的
非喚醒 turn 比例」。比例 < gate_pct → 建議不做 Phase B（多數閒聊太短，daemon 在
deferred-start 模式來不及幫上，VAD 已先切）。

用法：python scripts/analyze_volatile_value_window.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

INPUT = Path("records/volatile_shadow.jsonl")
MIN_MS = 1800        # deferred-start：daemon 要先讓 wake-check 窗（~1.8s）過才接手
GATE_PCT = 30.0      # 價值窗比例門檻
MIN_SAMPLES = 30     # 低於此不下結論


def analyze_value_window(rows: list[dict], *, min_ms: int = MIN_MS,
                         gate_pct: float = GATE_PCT, min_samples: int = MIN_SAMPLES) -> dict:
    non_wake = [
        r for r in rows
        if not r.get("error") and r.get("wake_first_ms") is None
        and isinstance(r.get("audio_ms"), (int, float))
    ]
    total = len(non_wake)
    in_window = sum(1 for r in non_wake if r["audio_ms"] >= min_ms)
    pct = round(in_window / total * 100, 1) if total else 0.0

    if total < min_samples:
        verdict = "insufficient_data"
    elif pct < gate_pct:
        verdict = "skip_phase_b"
    else:
        verdict = "proceed_phase_b"

    durations = sorted(r["audio_ms"] for r in non_wake)
    p50 = durations[len(durations) // 2] if durations else 0
    return {
        "non_wake_total": total,
        "in_window": in_window,
        "in_window_pct": pct,
        "min_ms": min_ms,
        "gate_pct": gate_pct,
        "p50_audio_ms": p50,
        "verdict": verdict,
    }


def main() -> int:
    if not INPUT.exists():
        print(f"input not found: {INPUT}", file=sys.stderr)
        return 1
    rows = []
    for line in INPUT.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    result = analyze_value_window(rows)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n[Plan 12 Phase A] verdict: {result['verdict']} "
          f"({result['in_window']}/{result['non_wake_total']} 非喚醒 turn 在價值窗, "
          f"p50={result['p50_audio_ms']}ms)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
