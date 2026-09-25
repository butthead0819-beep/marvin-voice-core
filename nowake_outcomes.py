"""nowake 分派（不喊喚醒詞）的 IntentBus 結果紀錄。

judge_outcomes.jsonl 只記 regex 分派且有 12+ 下游分析腳本，nowake 另開一檔不污染它。
原文欄位（raw_text / query）由 scripts/scrub_improvement_raw.py 在 14 天後轉 sha1（寬放 ZDR）。
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path("records/nowake_outcomes.jsonl")
SCHEMA_VERSION = 1


def build_nowake_row(ctx, bids, winner, now: float | None = None) -> dict:
    """純函式：組一筆紀錄。bids 依 confidence 由高到低排序後輸出。"""
    ordered = sorted(bids or [], key=lambda b: b.confidence, reverse=True)
    return {
        "ts": time.time() if now is None else now,
        "speaker": ctx.speaker,
        "raw_text": ctx.raw_text or "",
        "query": ctx.query or "",
        "winner": winner.name if winner is not None else None,
        "winner_confidence": winner.confidence if winner is not None else None,
        "bids": [{"name": b.name, "confidence": b.confidence, "reason": b.reason} for b in ordered],
        "schema_version": SCHEMA_VERSION,
    }


def append_nowake_outcome(ctx, bids, winner, path: Path | str = DEFAULT_PATH,
                          now: float | None = None) -> None:
    """Append 一行 jsonl。任何錯誤只記 debug，不影響分派。"""
    # 防遙測污染：pytest 下 relative 路徑 = 會寫進 prod records/，直接 no-op（同 intent_judges/telemetry.py）
    if os.environ.get("PYTEST_CURRENT_TEST") and not Path(path).is_absolute():
        return
    try:
        row = build_nowake_row(ctx, bids, winner, now=now)
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False))
            f.write("\n")
    except Exception as exc:
        logger.debug(f"[nowake_outcomes] 寫入失敗（不影響分派）: {exc}")
