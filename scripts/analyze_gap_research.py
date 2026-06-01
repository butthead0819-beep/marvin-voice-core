"""gap_research.jsonl 離線分析 — 免喚醒資訊真空偵測的 daily ritual 計數。

每筆 = 一次 pre-gate 放行後的 LLM 偵測；query 非 null = 命中真資訊真空（shadow 不交付）。
shadow 跑一週後看 hit 數 + query 品質，定奪是否值得建 Phase 2（research + 靜默交付）。

用法：python scripts/analyze_gap_research.py
輸出 JSON 到 stdout（對齊 analyze_agent_gaps / analyze_judge_outcomes）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

INPUT = Path("records/gap_research.jsonl")


def load(path: Path) -> list[dict]:
    rows: list[dict] = []
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def analyze(rows: list[dict]) -> dict:
    escalations = len(rows)
    hits = [r for r in rows if r.get("query")]
    by_mode: dict[str, int] = {}
    for r in rows:
        m = r.get("mode", "unknown")
        by_mode[m] = by_mode.get(m, 0) + 1
    return {
        "escalations": escalations,
        "gap_hits": len(hits),
        "hit_rate": round(len(hits) / escalations, 4) if escalations else 0.0,
        "by_mode": by_mode,
        "sample_queries": [r["query"] for r in hits][:15],
    }


def main() -> int:
    if not INPUT.exists():
        print(json.dumps({"escalations": 0, "note": f"{INPUT} 尚無資料（shadow 未開或無觸發）"},
                         ensure_ascii=False))
        return 0
    print(json.dumps(analyze(load(INPUT)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
