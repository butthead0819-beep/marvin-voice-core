#!/usr/bin/env python
"""每週生成口味指紋 + 漂移 → records/taste_fingerprint.json，並印摘要。

deterministic（純統計、無 LLM），跟 build_taste_profiles.py（LLM 鄰近 seed）互補。
排程：每週一次（見 com.antigravity.marvin.tastefingerprint.plist）。手動跑：
    venv_simon/bin/python scripts/build_taste_fingerprint.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from taste_fingerprint import compute_taste_fingerprint, diff_fingerprints

MM = BASE / "music_memory.json"
OUT = BASE / "records" / "taste_fingerprint.json"


def _load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> None:
    songs = _load_json(MM).get("songs", {})
    new = compute_taste_fingerprint(songs)
    prev = _load_json(OUT)
    drift = diff_fingerprints(prev, new)
    new["drift_vs_prev"] = drift

    OUT.write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 摘要（供 review / DM）──────────────────────────────────────────────
    lang = " / ".join(f"{k}{v:.0%}" for k, v in new["language"].items())
    top = "、".join(f"{a}({c})" for a, c in new["core_artists"][:8])
    print(f"🎯 [口味指紋] {new['updated']}")
    print(f"   真人點播 {new['total_human_requests']} 次 / {new['distinct_songs']} 首")
    print(f"   語言：{lang}")
    print(f"   核心藝人：{top}")
    for u, info in list(new["per_user"].items())[:5]:
        arts = "、".join(f"{a}({c})" for a, c in info["core_artists"][:3])
        print(f"   - {u}（{info['requests']}次）：{arts}")
    if not prev.get("core_artists"):
        print("   （首次建立基準，下週起顯示漂移）")
    else:
        if drift.get("new_core_artists"):
            print(f"   📈 新進核心：{'、'.join(drift['new_core_artists'])}")
        if drift.get("dropped_core_artists"):
            print(f"   📉 掉出核心：{'、'.join(drift['dropped_core_artists'])}")
        if drift.get("language_shift"):
            print(f"   🌐 語言變化：{drift['language_shift']}")
    print(f"   → {OUT.relative_to(BASE)}")


if __name__ == "__main__":
    main()
