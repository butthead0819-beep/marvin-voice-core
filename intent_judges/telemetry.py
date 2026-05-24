"""Judge race outcome → records/judge_outcomes.jsonl (append-only).

外部分析（J1 hit rate / fast-path 比率 / 各 judge latency 分布）直接 jq / pandas
吃，所以序列化 schema 是契約 —— 動 schema 要同步動 replay tooling。

race.py 本身保持純函數（不寫檔），這個 module 才碰 FS。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from intent_bus import IntentContext
from intent_judges.race import RaceResult


def serialize_outcome(
    utterance_id: str,
    ctx: IntentContext,
    result: RaceResult,
) -> dict:
    """RaceResult → flat dict ready for json.dumps。Schema 是 jsonl 契約。"""
    return {
        "utterance_id": utterance_id,
        "ts": time.time(),
        "speaker": ctx.speaker,
        "mode": ctx.mode,
        "raw_query": ctx.query,
        "winning_judge": result.winning_judge,
        "winner_name": result.winner.name,
        "winner_confidence": result.winner.confidence,
        "winner_reason": result.winner.reason,
        "total_ms": result.total_ms,
        "judges": [
            {
                "name": o.name,
                "status": o.status,
                "latency_ms": o.latency_ms,
                "confidence": o.bid.confidence if o.bid is not None else None,
                "bid_name": o.bid.name if o.bid is not None else None,
                "bid_reason": o.bid.reason if o.bid is not None else None,
                "error": o.error,
            }
            for o in result.outcomes
        ],
    }


def write_race_outcome(
    path: Path,
    utterance_id: str,
    ctx: IntentContext,
    result: RaceResult,
) -> None:
    """Append 一行 jsonl 到 path（parent dir 不存在就建）。

    `ensure_ascii=False` 保繁中原樣，方便人眼讀 + 直接 jq。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    row = serialize_outcome(utterance_id, ctx, result)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False))
        f.write("\n")
