"""Judge outcomes 離線分析 — shadow race 數據量化。

讀 records/judge_outcomes.jsonl，輸出：
  - J1 fast-path 率（winning_judge=j1_regex 且 confidence >= threshold）
  - J1/J3 winner agent 一致率
  - J1 / J3 p50 / p95 latency
  - winner_name (agent) histogram
  - "J1=guard 但 J3 有 intent" 的 case 列表（race-rule 改善依據）
  - weak_play_curation 0.85 卡 threshold 的 case 數（threshold 0.85 提案）
  - 全部兩 judge 都 dense-zero 的 case 列表（missed intent / noise）

用途：每天/每週跑一次，把報告貼到 records/judge_outcomes_analysis_<date>.md
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from statistics import median

INPUT = Path("records/judge_outcomes.jsonl")
J1_THRESHOLD = 0.90  # 當前 production threshold；改 0.85 後重跑可比較


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(len(values) - 1, int(len(values) * p))
    return values[idx]


def load() -> list[dict]:
    rows: list[dict] = []
    with INPUT.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def analyze(rows: list[dict]) -> dict:
    total = len(rows)
    if not total:
        return {"total": 0}

    j1_lat: list[float] = []
    j3_lat: list[float] = []
    winner_names: Counter[str] = Counter()
    winning_judges: Counter[str] = Counter()

    j1_fastpath = 0  # winning_judge=j1_regex AND confidence >= J1_THRESHOLD
    both_dense_zero = []
    guard_with_j3_intent = []  # 議題 A
    weak_curation_at_threshold = []  # 議題 B
    j1_j3_agree = 0
    j1_j3_disagree = []

    for r in rows:
        winning_judges[r.get("winning_judge") or "_none_"] += 1
        winner_names[r.get("winner_name") or "_none_"] += 1

        judges = {j["name"]: j for j in r.get("judges", [])}
        j1 = judges.get("j1_regex")
        j3 = judges.get("j3_cleaner_precomputed")
        if j1:
            j1_lat.append(j1.get("latency_ms", 0))
        if j3:
            j3_lat.append(j3.get("latency_ms", 0))

        if (
            r.get("winning_judge") == "j1_regex"
            and (r.get("winner_confidence") or 0) >= J1_THRESHOLD
        ):
            j1_fastpath += 1

        if j1 and j3:
            j1_zero = (j1.get("confidence") or 0) < 0.30
            j3_zero = (j3.get("confidence") or 0) < 0.30
            if j1_zero and j3_zero:
                both_dense_zero.append(r["raw_query"])
            elif j1.get("bid_name") == j3.get("bid_name"):
                j1_j3_agree += 1
            else:
                j1_j3_disagree.append({
                    "raw": r["raw_query"],
                    "j1": f"{j1.get('bid_name')}({j1.get('confidence'):.2f})",
                    "j3": f"{j3.get('bid_name')}({j3.get('confidence'):.2f})",
                })

            # 議題 A：J1=guard 但 J3 有非 guard 的真 intent
            if (
                j1.get("bid_name") == "guard"
                and j3.get("bid_name") not in (None, "guard", "cleaner_judge", "regex_judge")
                and (j3.get("confidence") or 0) >= 0.80
            ):
                guard_with_j3_intent.append({
                    "raw": r["raw_query"],
                    "j1_reason": j1.get("bid_reason"),
                    "j3": f"{j3.get('bid_name')}({j3.get('confidence'):.2f})",
                })

            # 議題 B：J1 curation 0.85 卡 threshold（兩 judge 一致）
            reason = j1.get("bid_reason") or ""
            if (
                "weak_play_curation" in reason
                and 0.84 <= (j1.get("confidence") or 0) <= 0.86
                and j3.get("bid_name") == j1.get("bid_name")
            ):
                weak_curation_at_threshold.append(r["raw_query"])

    completed_pairs = j1_j3_agree + len(j1_j3_disagree)
    return {
        "total": total,
        "j1_fastpath_rate": j1_fastpath / total,
        "j1_p50_ms": median(j1_lat) if j1_lat else 0,
        "j1_p95_ms": percentile(j1_lat, 0.95),
        "j3_p50_ms": median(j3_lat) if j3_lat else 0,
        "j3_p95_ms": percentile(j3_lat, 0.95),
        "winning_judges": dict(winning_judges),
        "winner_agents": dict(winner_names),
        "j1_j3_agree_rate": (j1_j3_agree / completed_pairs) if completed_pairs else 0,
        "j1_j3_agree_count": j1_j3_agree,
        "j1_j3_disagree": j1_j3_disagree,
        "both_dense_zero_count": len(both_dense_zero),
        "both_dense_zero_samples": both_dense_zero[:10],
        "guard_with_j3_intent": guard_with_j3_intent,
        "weak_curation_at_threshold_count": len(weak_curation_at_threshold),
        "weak_curation_at_threshold_samples": weak_curation_at_threshold[:10],
    }


def main() -> int:
    if not INPUT.exists():
        print(f"input not found: {INPUT}", file=sys.stderr)
        return 1
    rows = load()
    result = analyze(rows)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
