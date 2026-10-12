"""組語料：anchors（來自 MUSIC_*_KW）+ 換句話說的指令 + 近似反例 + 閒聊 + 可選的真實逐字稿。

CLI：python -m scripts.voice_embed_bench.corpus [--out ...] [--transcripts-db ...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sqlite3
from collections import Counter
from pathlib import Path

ACTIONS = ("skip", "pause", "resume", "stop")
_HAN = re.compile(r"[一-鿿]")
_HERE = Path(__file__).parent


def load_anchors() -> dict[str, list[str]]:
    """取 MUSIC_*_KW，只保留含漢字者（英文關鍵字不適合拿來比對中文語音）。"""
    from intent_agents.constants import (
        MUSIC_PAUSE_KW, MUSIC_RESUME_KW, MUSIC_SKIP_KW, MUSIC_STOP_KW,
    )
    src = {
        "skip": MUSIC_SKIP_KW,
        "pause": MUSIC_PAUSE_KW,
        "resume": MUSIC_RESUME_KW,
        "stop": MUSIC_STOP_KW,
    }
    return {a: [k for k in src[a] if _HAN.search(k)] for a in ACTIONS}


def load_phrases(path=_HERE / "phrases.json") -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_corpus(phrases: dict, anchors: dict[str, list[str]],
                 transcripts: list[str] | None = None) -> list[dict]:
    """依序加入並以 text.strip() 去重，先出現者勝。"""
    entries: list[tuple[str, str, str | None]] = []
    for action, texts in anchors.items():
        entries += [(t, "anchor", action) for t in texts]
    for action, texts in phrases["positives"].items():
        entries += [(t, "paraphrase", action) for t in texts]
    entries += [(t, "near_miss", None) for t in phrases["negatives_near_miss"]]
    entries += [(t, "chat", None) for t in phrases["negatives_chat"]]
    entries += [(t, "transcript", None) for t in (transcripts or [])]

    seen: set[str] = set()
    out: list[dict] = []
    for text, kind, label in entries:
        key = text.strip()
        if key in seen:
            continue
        seen.add(key)
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
        out.append({"id": f"{kind}-{digest}", "text": text, "label": label, "kind": kind})
    return out


def load_transcripts(db_path, limit: int, seed: int,
                     anchors: dict[str, list[str]]) -> list[str]:
    """從 sqlite transcripts 表抽閒聊句（排除指令句），固定 seed 可重現。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT text FROM transcripts").fetchall()
    finally:
        conn.close()

    try:
        from command_fastpath import match_command_action
    except ImportError:
        match_command_action = None

    anchor_strs = [a for texts in anchors.values() for a in texts]
    seen: set[str] = set()
    kept: list[str] = []
    for (raw,) in rows:
        if not raw:
            continue
        text = raw.strip()
        if not 2 <= len(text) <= 30 or text in seen:
            continue
        if any(a in text for a in anchor_strs):
            continue
        if match_command_action is not None and match_command_action(text) is not None:
            continue
        seen.add(text)
        kept.append(text)
    return random.Random(seed).sample(kept, min(limit, len(kept)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="records/voice_embed_bench/corpus.jsonl")
    ap.add_argument("--transcripts-db", default=None)
    ap.add_argument("--transcripts-limit", type=int, default=300)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    anchors = load_anchors()
    transcripts = None
    if args.transcripts_db:
        transcripts = load_transcripts(args.transcripts_db, args.transcripts_limit,
                                       args.seed, anchors)
    items = build_corpus(load_phrases(), anchors, transcripts)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    for kind, n in Counter(it["kind"] for it in items).items():
        print(f"{kind}: {n}")
    print(f"total: {len(items)} → {out}")


if __name__ == "__main__":
    main()
