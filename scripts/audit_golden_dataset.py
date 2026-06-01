"""suki_golden_dataset.jsonl 健康度 audit — Operation Distillation 的蒸餾前置 gate。

為什麼是 audit 而非 replay-eval（2026-06-01 發現）：
golden 由 gemini_router_content 兩個呼叫點累積（社交分析 JSON + 補位台詞自由文本），
但 social-analysis 的 output schema 飄移嚴重（15+ 欄位組合）、值髒（string bool
`"True"`、pipe enum `"info|neutral|none"`）、混入退化樣本（`{}`、`{"type":"object"}`）
與 `__META__` 污染。正規化前 replay 比對 = 拿髒 ground truth 當基準。
本腳本純函式統計「可蒸餾比例」，零 LLM、可進 3am batch（best-effort）。

用法：python scripts/audit_golden_dataset.py
輸出 JSON 到 stdout（與 analyze_agent_gaps / analyze_judge_outcomes 對齊）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

INPUT = Path("records/suki_golden_dataset.jsonl")

# social-analysis 視為「乾淨可蒸餾」必須無這些 dirty flag
_BLOCKING_DIRTY = {"string_bool", "pipe_enum", "field_name_drift", "missing_social_gap"}


def load(path: Path) -> list[dict]:
    """容忍壞行：跳過空行與無法 parse 的行，不丟例外（batch best-effort）。"""
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


def _content(rec: dict, role: str) -> str | None:
    for m in rec.get("messages", []):
        if m.get("role") == role:
            return m.get("content")
    return None


def _try_json(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _is_polluted(text: str | None) -> bool:
    return bool(text) and "__META__" in text


def _is_degenerate(parsed: dict) -> bool:
    return not parsed or set(parsed.keys()) <= {"type"}


def classify_record(rec: dict) -> str:
    """五分類：social_analysis / freetext / degenerate / polluted / unparseable。"""
    user = _content(rec, "user")
    asst = _content(rec, "assistant")
    if _is_polluted(user) or _is_polluted(asst):
        return "polluted"
    if asst is None:
        return "unparseable"
    parsed = _try_json(asst)
    if parsed is None:
        return "freetext"  # 非 JSON → 補位台詞
    if not isinstance(parsed, dict):
        return "unparseable"
    if _is_degenerate(parsed):
        return "degenerate"
    if "social_gap" in parsed or "intervention_confidence" in parsed:
        return "social_analysis"
    return "unparseable"


def flag_dirty(parsed: dict) -> set[str]:
    """偵測 social-analysis dict 的髒值。回傳 flag 集合（乾淨 = 空集合）。"""
    flags: set[str] = set()
    if isinstance(parsed.get("intervention_decision"), str):
        flags.add("string_bool")
    if any(isinstance(v, str) and "|" in v for v in parsed.values()):
        flags.add("pipe_enum")
    if "intervention_confidence" in parsed and "confidence" not in parsed:
        flags.add("field_name_drift")
    if "social_gap" not in parsed:
        flags.add("missing_social_gap")
    return flags


def count_exact_duplicates(rows: list[dict]) -> int:
    """完全相同的 messages 內容 = 重複污染。回傳「超出首次出現」的筆數。"""
    seen: set[str] = set()
    dups = 0
    for r in rows:
        key = json.dumps(r.get("messages", []), ensure_ascii=False, sort_keys=True)
        if key in seen:
            dups += 1
        else:
            seen.add(key)
    return dups


def audit(rows: list[dict]) -> dict:
    total = len(rows)
    categories = {
        "social_analysis": 0, "freetext": 0,
        "degenerate": 0, "polluted": 0, "unparseable": 0,
    }
    dirty_tally: dict[str, int] = {}
    schema_variants: dict[str, int] = {}
    clean_usable = 0

    for rec in rows:
        cat = classify_record(rec)
        categories[cat] += 1
        if cat != "social_analysis":
            continue
        parsed = _try_json(_content(rec, "assistant")) or {}
        variant = ",".join(sorted(parsed.keys()))
        schema_variants[variant] = schema_variants.get(variant, 0) + 1
        flags = flag_dirty(parsed)
        for fl in flags:
            dirty_tally[fl] = dirty_tally.get(fl, 0) + 1
        if not (flags & _BLOCKING_DIRTY):
            clean_usable += 1

    top_variants = sorted(schema_variants.items(), key=lambda kv: kv[1], reverse=True)[:10]

    return {
        "total": total,
        "categories": categories,
        "duplicates": {"exact_dup_records": count_exact_duplicates(rows)},
        "social_analysis": {
            "count": categories["social_analysis"],
            "schema_variant_count": len(schema_variants),
            "top_schema_variants": [{"fields": k, "count": v} for k, v in top_variants],
            "dirty": dirty_tally,
            "clean_usable": clean_usable,
        },
        "verdict": {
            "distillation_ready": clean_usable,
            "usable_ratio": round(clean_usable / total, 4) if total else 0.0,
        },
    }


def main() -> int:
    if not INPUT.exists():
        print(f"input not found: {INPUT}", file=sys.stderr)
        return 1
    result = audit(load(INPUT))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
