"""collision_reaction_report.py — 聊天撞歌詞有沒有讓大家多講話？

讀 records/dj_narration.jsonl：
- 命中率：有歌詞的口白中，literal／pinyin_only 撞點各幾筆。
- 反應：collision 上播組 vs holdout 組，口白後 60 秒發言數 − 口白前 60 秒發言數。
  兩組都只算完整播出（aired kind=full）且有 reaction 紀錄的口白。

用法：
    python scripts/collision_reaction_report.py
    python scripts/collision_reaction_report.py --since 2026-10-10
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "records" / "dj_narration.jsonl"


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict):
                    rows.append(rec)
    except Exception:
        pass
    return rows


def _group(deltas: list[int]) -> dict:
    return {"n": len(deltas), "mean_delta": sum(deltas) / len(deltas) if deltas else None}


def summarize(rows: list[dict], *, since_ts: float = 0.0) -> dict:
    narrations = [r for r in rows if "type" not in r and r.get("narration_id")
                  and (r.get("ts") or 0) >= since_ts]
    full_aired = {r["narration_id"] for r in rows
                  if r.get("type") == "aired" and r.get("kind") == "full"}
    reactions = {r["narration_id"]: r["post_60"] - r["pre_60"] for r in rows
                 if r.get("type") == "reaction" and r.get("narration_id")}

    with_lyrics = [r for r in narrations if r.get("has_lyrics")]
    aired, holdout = [], []
    for r in narrations:
        nid = r["narration_id"]
        if nid not in full_aired or nid not in reactions:
            continue
        if r.get("collision_aired"):
            aired.append(reactions[nid])
        elif r.get("collision_holdout"):
            holdout.append(reactions[nid])
    return {
        "with_lyrics": len(with_lyrics),
        "literal": sum(1 for r in with_lyrics if r.get("collision_kind") == "literal"),
        "pinyin_only": sum(1 for r in with_lyrics if r.get("collision_kind") == "pinyin_only"),
        "aired": _group(aired),
        "holdout": _group(holdout),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="只算這天之後的口白（YYYY-MM-DD）")
    args = parser.parse_args()
    since_ts = datetime.strptime(args.since, "%Y-%m-%d").timestamp() if args.since else 0.0

    s = summarize(_read_jsonl(LOG_PATH), since_ts=since_ts)
    n = s["with_lyrics"]
    rate = f"{s['literal'] / n:.1%}" if n else "-"
    print(f"有歌詞的口白：{n}　literal 撞點：{s['literal']}（{rate}）　pinyin_only：{s['pinyin_only']}")
    for name in ("aired", "holdout"):
        g = s[name]
        mean = f"{g['mean_delta']:+.2f}" if g["mean_delta"] is not None else "-"
        print(f"{name:<8} n={g['n']:<4} 口白後−口白前 60 秒發言數 平均 {mean}")


if __name__ == "__main__":
    main()
