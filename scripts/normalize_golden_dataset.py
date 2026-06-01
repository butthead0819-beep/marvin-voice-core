"""golden social-analysis 投影到最小共同 schema — Operation Distillation 正規化器。

最小共同 schema = {social_gap, confidence, sentiment}（2026-06-01 拍板 3-key）。
做三件事：(1) 統一欄位名 intervention_confidence→confidence (2) 修值型別
（pipe enum 取首 token、confidence clamp 0..1、sentiment 同義詞收斂、social_gap
同義詞收斂 info→information_backup 等）(3) 去重（相同 user+output）。
社交分析以外、social_gap 缺漏、confidence 不可解析 → drop。

砍 intervention：最大變體（404 筆）無此欄、整體 65% null，留著只是弱訊號 + null 雜訊。
收斂 social_gap：縮寫版（info/redir/emo）與全名版同概念，不統一會教模型兩套標籤。

輸出 records/suki_golden_normalized.jsonl（不覆蓋原始）。stdout 印 stats JSON。
用法：python scripts/normalize_golden_dataset.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

INPUT = Path("records/suki_golden_dataset.jsonl")
OUTPUT = Path("records/suki_golden_normalized.jsonl")

_SENTIMENT_MAP = {
    "neg": "negative", "negative": "negative",
    "pos": "positive", "positive": "positive",
    "neutral": "neutral",
}

# 縮寫版 → 全名版（同概念收斂）
_SOCIAL_GAP_MAP = {
    "info": "information_backup",
    "redir": "subject_redirect",
    "emo": "emotional_support",
}


def canon_social_gap(v) -> str | None:
    if not isinstance(v, str):
        return None
    first = v.split("|")[0].strip()
    if not first:
        return None
    return _SOCIAL_GAP_MAP.get(first, first)


def canon_confidence(parsed: dict) -> float | None:
    raw = parsed.get("confidence", parsed.get("intervention_confidence"))
    if raw is None:
        return None
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, f))


def canon_sentiment(v) -> str:
    if isinstance(v, str):
        return _SENTIMENT_MAP.get(v.strip().lower(), "neutral")
    return "neutral"


def _content(rec: dict, role: str) -> str | None:
    for m in rec.get("messages", []):
        if m.get("role") == role:
            return m.get("content")
    return None


def normalize_record(rec: dict) -> dict | None:
    """投影到最小共同 schema。不可用（freetext/退化/缺必要欄位）→ None。"""
    asst = _content(rec, "assistant")
    if not isinstance(asst, str):
        return None
    try:
        parsed = json.loads(asst)
    except (json.JSONDecodeError, TypeError):
        return None  # freetext
    if not isinstance(parsed, dict):
        return None

    social_gap = canon_social_gap(parsed.get("social_gap"))
    confidence = canon_confidence(parsed)
    if social_gap is None or confidence is None:
        return None  # 必要欄位缺漏

    normalized = {
        "social_gap": social_gap,
        "confidence": confidence,
        "sentiment": canon_sentiment(parsed.get("sentiment")),
    }
    msgs = [m for m in rec.get("messages", []) if m.get("role") in ("system", "user")]
    msgs.append({"role": "assistant", "content": json.dumps(normalized, ensure_ascii=False)})
    return {"messages": msgs}


def normalize_dataset(rows: list[dict]) -> tuple[list[dict], dict]:
    out: list[dict] = []
    seen: set[str] = set()
    dropped_unusable = 0
    dropped_duplicate = 0
    for rec in rows:
        norm = normalize_record(rec)
        if norm is None:
            dropped_unusable += 1
            continue
        key = json.dumps(
            [m["content"] for m in norm["messages"] if m["role"] in ("user", "assistant")],
            ensure_ascii=False,
        )
        if key in seen:
            dropped_duplicate += 1
            continue
        seen.add(key)
        out.append(norm)
    stats = {
        "input": len(rows),
        "written": len(out),
        "dropped_unusable": dropped_unusable,
        "dropped_duplicate": dropped_duplicate,
    }
    return out, stats


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


def main() -> int:
    if not INPUT.exists():
        print(f"input not found: {INPUT}", file=sys.stderr)
        return 1
    out, stats = normalize_dataset(_load(INPUT))
    with OUTPUT.open("w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    stats["output_path"] = str(OUTPUT)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
