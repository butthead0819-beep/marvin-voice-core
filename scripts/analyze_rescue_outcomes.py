"""Rescue outcomes 離線分析 — LLM rescue pipeline 的 daily ritual。

讀 records/rescue_outcomes.jsonl（由 IntentBus rescue_outcome_sink 寫入），
輸出四個分析切片：

  - by_gap_class           : convergent/divergent/unmatched/shadow 計數
  - convergent_clusters    : 依 (winner_agent, winner_reason) 聚類，count≥2
                             即 ready_to_propose（regex 擴充候選）
  - divergent_by_target    : 按 pragmatic_target → signal → count + samples
                             分組，餵推薦扣分用
  - unmatched_samples      : LLM 也救不回來的孤兒（前 N 樣），落回 agent_gaps
                             phase A.5 clustering 路徑
  - shadow_samples         : 校準週用，人工看 LLM 改寫品質（original→rewritten）

用途：每天跑一次，把 JSON 報告貼到 records/rescue_analysis_<date>.md。

設計（與 analyze_judge_outcomes.py 對齊）：
- load(path) 與 analyze(rows) 純函式分離，便於單元測試
- 不寫檔，stdout 印 JSON；caller（run_daily_review 或人工）自行 pipe
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

INPUT = Path("records/rescue_outcomes.jsonl")
READY_TO_PROPOSE_THRESHOLD = 2  # 使用者拍板的 intent gap 升級門檻（激進補 agent 偏好）
SAMPLE_CAP = 10                 # unmatched / divergent samples 每組上限
SHADOW_SAMPLE_CAP = 20          # 校準週要看比較多


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
    if not total:
        return {"total": 0, "by_gap_class": {}}

    by_gap_class: Counter[str] = Counter()
    convergent_buckets: dict[tuple[str, str], dict] = {}
    divergent_buckets: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(
        lambda: {"count": 0, "samples": []}
    ))
    unmatched_samples: list[str] = []
    unmatched_total = 0
    shadow_samples: list[dict] = []

    for r in rows:
        gc = r.get("gap_class") or "_unknown"
        by_gap_class[gc] += 1

        if gc == "convergent":
            key = (r.get("winner_agent") or "_none", r.get("winner_reason") or "_none")
            bucket = convergent_buckets.setdefault(key, {
                "winner_agent": key[0],
                "winner_reason": key[1],
                "count": 0,
                "samples": [],
            })
            bucket["count"] += 1
            if len(bucket["samples"]) < SAMPLE_CAP:
                bucket["samples"].append(r.get("original_query", ""))

        elif gc == "divergent":
            target = r.get("pragmatic_target") or "_unspecified"
            signal = r.get("pragmatic_signal") or "_unspecified"
            slot = divergent_buckets[target][signal]
            slot["count"] += 1
            if len(slot["samples"]) < SAMPLE_CAP:
                slot["samples"].append(r.get("original_query", ""))

        elif gc == "unmatched":
            unmatched_total += 1
            if len(unmatched_samples) < SAMPLE_CAP:
                unmatched_samples.append(r.get("original_query", ""))

        elif gc == "shadow":
            if len(shadow_samples) < SHADOW_SAMPLE_CAP:
                shadow_samples.append({
                    "original_query": r.get("original_query"),
                    "rewritten_query": r.get("rewritten_query"),
                    "pragmatic_signal": r.get("pragmatic_signal"),
                    "pragmatic_target": r.get("pragmatic_target"),
                })

    convergent_clusters = sorted(
        convergent_buckets.values(),
        key=lambda c: c["count"],
        reverse=True,
    )
    for c in convergent_clusters:
        c["ready_to_propose"] = c["count"] >= READY_TO_PROPOSE_THRESHOLD

    divergent_by_target = {
        target: {signal: dict(slot) for signal, slot in signals.items()}
        for target, signals in divergent_buckets.items()
    }

    return {
        "total": total,
        "by_gap_class": dict(by_gap_class),
        "convergent_clusters": convergent_clusters,
        "divergent_by_target": divergent_by_target,
        "unmatched_total": unmatched_total,
        "unmatched_samples": unmatched_samples,
        "shadow_samples": shadow_samples,
    }


def main() -> int:
    if not INPUT.exists():
        print(f"input not found: {INPUT}", file=sys.stderr)
        return 1
    rows = load(INPUT)
    result = analyze(rows)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
