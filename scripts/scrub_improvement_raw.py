"""寬放 ZDR：judge/gaps/rescue 的 raw 原文過 TTL 轉單向 hash 指紋。

為何 hash 而非清空（2026-06-01 設計決策）：
analyze_agent_gaps 的 distinct 計數 key on raw_query。直接清空會讓所有舊記錄塌成
同一 key、汙染 distinct/plan-trigger。改存 sha1 指紋 → 可讀原文消失（ZDR 達標），
但相同原文 → 相同 hash，distinct/dedup 相等性保留、分析不失準。
代價：>TTL 舊資料無法再語意 clustering（clustering 跑近期累積，TTL 給足窗口）。

TTL 預設 14 天（🟡 失敗訊號低頻、clustering 要跨天累積 ≥5；不可壓到 24h）。
冪等：已 scrub（值帶 SCRUB_PREFIX）的記錄重跑不再處理。

用法：python scripts/scrub_improvement_raw.py
寫回原檔（atomic：先寫 .tmp 再 rename）。輸出 JSON 摘要到 stdout。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

SCRUB_PREFIX = "scrubbed:sha1:"
TTL_DAYS = 14

# 各檔需 scrub 的原文欄位
TARGETS: list[tuple[str, list[str]]] = [
    ("records/agent_gaps.jsonl", ["raw_query", "cleaned_query"]),
    ("records/judge_outcomes.jsonl", ["raw_query"]),
    ("records/rescue_outcomes.jsonl", ["original_query", "rewritten_query"]),
]


def scrub_value(text: str) -> str:
    """原文 → 單向指紋。已是指紋則原樣回傳（冪等）。"""
    if text.startswith(SCRUB_PREFIX):
        return text
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    return SCRUB_PREFIX + digest


def scrub_rows(
    rows: list[dict], text_fields: list[str], cutoff_ts: float
) -> tuple[list[dict], int]:
    """ts < cutoff 的記錄中，把指定文字欄位轉指紋。回傳 (rows, 被 scrub 的欄位數)。"""
    scrubbed = 0
    for r in rows:
        if r.get("ts", float("inf")) >= cutoff_ts:
            continue
        for field in text_fields:
            val = r.get(field)
            if not isinstance(val, str) or not val:
                continue
            new = scrub_value(val)
            if new != val:
                r[field] = new
                scrubbed += 1
    return rows, scrubbed


def _load(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _atomic_write(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def main() -> int:
    cutoff = time.time() - TTL_DAYS * 86400
    summary: dict[str, dict] = {}
    for rel, fields in TARGETS:
        path = Path(rel)
        if not path.exists():
            summary[rel] = {"status": "missing"}
            continue
        rows = _load(path)
        rows, n = scrub_rows(rows, fields, cutoff)
        if n:
            _atomic_write(path, rows)
        summary[rel] = {"records": len(rows), "scrubbed_fields": n}
    print(json.dumps({"ttl_days": TTL_DAYS, "files": summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
