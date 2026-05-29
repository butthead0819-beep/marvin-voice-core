"""Agent gaps 離線分析 — Plan 4 Intent Gap 的 daily ritual 計數工具。

讀 records/agent_gaps.jsonl，按 **distinct (speaker, raw_query)** 算 occurrence，
排除 UNKNOWN，distinct_count ≥ 2 標 ready_to_implement。

為什麼 dedup 是核心（2026-05-30 教訓）：
同一句重複 N 次（QA 連發 / 結巴 / 跳針）若用 raw line count 會灌爆門檻
（buy_milk/replay_user_history 各 7 筆全同句，假觸發 ≥5）。distinct 計數讓
「累計 2 次」回到原意 = 兩個不同 occurrence，不是同句 2 次。

threshold=2：feedback_intent_gap_threshold.md，使用者拍板激進補 agent。

用法：python scripts/analyze_agent_gaps.py
輸出 JSON 到 stdout（與 analyze_judge_outcomes / analyze_rescue_outcomes 對齊）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

INPUT = Path("records/agent_gaps.jsonl")
READY_THRESHOLD = 2  # distinct occurrence 門檻


def load(path: Path) -> list[dict]:
    rows: list[dict] = []
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def analyze(rows: list[dict]) -> dict:
    total = len(rows)
    non_unknown = [r for r in rows if (r.get("intent_type") or "UNKNOWN") != "UNKNOWN"]

    by_type: dict[str, dict] = {}
    for r in non_unknown:
        it = r["intent_type"]
        bucket = by_type.setdefault(it, {"raw_count": 0, "distinct": set(), "samples": []})
        bucket["raw_count"] += 1
        bucket["distinct"].add((r.get("speaker", ""), r.get("raw_query", "")))
        raw = r.get("raw_query", "")
        if raw and raw not in bucket["samples"]:
            bucket["samples"].append(raw)

    intents = []
    for it, b in by_type.items():
        distinct_count = len(b["distinct"])
        intents.append({
            "intent_type": it,
            "raw_count": b["raw_count"],
            "distinct_count": distinct_count,
            "ready_to_implement": distinct_count >= READY_THRESHOLD,
            "samples": b["samples"][:5],
        })
    intents.sort(key=lambda x: (x["distinct_count"], x["raw_count"]), reverse=True)

    return {
        "total": total,
        "total_non_unknown": len(non_unknown),
        "intents": intents,
        "ready_count": sum(1 for i in intents if i["ready_to_implement"]),
    }


def main() -> int:
    if not INPUT.exists():
        print(f"input not found: {INPUT}", file=sys.stderr)
        return 1
    result = analyze(load(INPUT))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
