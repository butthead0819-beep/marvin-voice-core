#!/usr/bin/env python3
"""Plan 12 Phase A kill-gate：非喚醒語句價值窗分析。

讀 records/volatile_shadow.jsonl，算「落在串流早切價值窗（audio_ms ≥ min_ms）的
非喚醒 turn 比例」。比例 < gate_pct → 建議不做 Phase B（多數閒聊太短，daemon 在
deferred-start 模式來不及幫上，VAD 已先切）。

用法：python scripts/analyze_volatile_value_window.py
"""
from __future__ import annotations

import glob
import json
import re
import sys
from pathlib import Path

INPUT = Path("records/volatile_shadow.jsonl")
_AUDIT_RE = re.compile(r"Audio Audit.*長度: ([0-9.]+)s")
MIN_MS = 1800        # deferred-start：daemon 要先讓 wake-check 窗（~1.8s）過才接手
GATE_PCT = 30.0      # 價值窗比例門檻
MIN_SAMPLES = 30     # 低於此不下結論


def parse_audit_durations(lines: list[str]) -> list[float]:
    """從 bot log 的 Audio Audit 行抽句長（ms）。歷史高量數據源（每句都記，
    不像 volatile_shadow 需重新累積）。缺點：不分 wake/非喚醒（見 analyze 註）。"""
    out: list[float] = []
    for line in lines:
        m = _AUDIT_RE.search(line)
        if m:
            try:
                out.append(float(m.group(1)) * 1000.0)
            except ValueError:
                continue
    return out


def analyze_audit_window(durations_ms: list[float], *, min_ms: int = MIN_MS,
                         gate_pct: float = GATE_PCT, min_samples: int = MIN_SAMPLES) -> dict:
    """從 Audio Audit 句長分佈算價值窗比例。涵蓋全部句（含喚醒命令），是 proceed/skip
    的高量粗估；精確 wake-split 用 volatile_shadow（analyze_value_window）。"""
    total = len(durations_ms)
    in_window = sum(1 for d in durations_ms if d >= min_ms)
    pct = round(in_window / total * 100, 1) if total else 0.0
    if total < min_samples:
        verdict = "insufficient_data"
    elif pct < gate_pct:
        verdict = "skip_phase_b"
    else:
        verdict = "proceed_phase_b"
    s = sorted(durations_ms)
    return {
        "source": "audit_log_all_utterances",
        "total": total,
        "in_window": in_window,
        "in_window_pct": pct,
        "min_ms": min_ms,
        "gate_pct": gate_pct,
        "p50_ms": round(s[len(s) // 2]) if s else 0,
        "verdict": verdict,
    }


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
    # 來源 1：volatile_shadow（精確 wake-split，但需累積）
    if INPUT.exists():
        rows = []
        for line in INPUT.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
        r = analyze_value_window(rows)
        print("=== 來源 A: volatile_shadow（精確 wake-split）===")
        print(json.dumps(r, ensure_ascii=False, indent=2))
        print(f"[A] verdict: {r['verdict']} ({r['in_window']}/{r['non_wake_total']} 非喚醒在窗, "
              f"p50={r['p50_audio_ms']}ms)\n", file=sys.stderr)

    # 來源 2：Audio Audit 歷史 log（高量粗估，含喚醒命令）
    audit_lines: list[str] = []
    for f in glob.glob("bot_stdout.log*") + glob.glob("bot_main.log*"):
        try:
            audit_lines.extend(Path(f).read_text(encoding="utf-8", errors="ignore").splitlines())
        except Exception:
            continue
    durs = parse_audit_durations(audit_lines)
    if durs:
        a = analyze_audit_window(durs)
        print("=== 來源 B: Audio Audit log（全句高量粗估）===")
        print(json.dumps(a, ensure_ascii=False, indent=2))
        print(f"[B] verdict: {a['verdict']} ({a['in_window']}/{a['total']} 在價值窗 ≥{a['min_ms']}ms, "
              f"p50={a['p50_ms']}ms)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
