#!/usr/bin/env python3
"""每日生成 LLM 品味 profile + 鄰近歌手 seed → records/taste_profiles.json。

autopilot T2 的「離線 biased expert」（[[triadic_expert_pattern_domain_and_timing]]）：
LLM 讀每人 liked/played 歌 → profile + adjacent_artists(破回音室) + avoid_artists(負空間)
→ ytmusic search 解析鄰近歌手成真 videoId（resolve-then-trust 防幻覺）→ 寫快取。
T2 runtime 只讀快取 videoId（LLM_TASTE_T2=on），語音熱路徑不打 LLM。

走 bus：daily batch → call_paid_review（[[feedback_llm_calls_must_use_bus]]）。
手動跑：venv_simon/bin/python scripts/build_taste_profiles.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

_CACHE = BASE / "records" / "taste_profiles.json"
_MIN_SONGS = 5            # 歌太少不值得打 LLM
_MAX_SONGS = 25           # prompt 上限
_MUSIC_LIKE_HINT = ("歌", "音樂", "曲", "團", "搖滾", "嘻哈", "電音", "金曲", "張", "周", "林")


def _load_env():
    f = BASE / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _gather(user: str, mm: dict, sk: dict) -> tuple[list[str], list[str]]:
    """回 (該使用者真人點過/聽過的歌名, 音樂相關興趣標籤)。"""
    titles: list[str] = []
    for _url, s in (mm.get("songs") or {}).items():
        reqs = s.get("requesters", {}) or {}
        if any(user in r and "Marvin" not in r and "推薦" not in r for r in reqs):
            t = s.get("title", "")
            if t:
                titles.append(t)
    p = (sk.get("players") or {}).get(user, {})
    titles += [t for t in (p.get("song_history") or []) if t]
    titles = list(dict.fromkeys(titles))[:_MAX_SONGS]
    likes = [l for l in (p.get("likes") or []) if any(h in l for h in _MUSIC_LIKE_HINT)]
    return titles, likes


async def main():
    _load_env()
    import taste_profile
    from llm_pool import call_paid_review
    from ytmusicapi import YTMusic

    mm = json.loads((BASE / "music_memory.json").read_text(encoding="utf-8"))
    sk = json.loads((BASE / "suki_memory.json").read_text(encoding="utf-8"))

    # 候選使用者：music_memory 出現過的真人 requester
    users: set[str] = set()
    for s in (mm.get("songs") or {}).values():
        for r in (s.get("requesters", {}) or {}):
            if "Marvin" not in r and "推薦" not in r:
                users.add(r)

    async def _call(content, system):
        return await call_paid_review(content, system=system, max_tokens=1200,
                                      temperature=0.4, timeout=90, caller="taste_profiles")

    yt = YTMusic()
    done = 0
    for user in sorted(users):
        titles, likes = _gather(user, mm, sk)
        if len(titles) < _MIN_SONGS:
            print(f"[Taste] {user}: 歌 {len(titles)} < {_MIN_SONGS}，跳過", flush=True)
            continue
        prof = await taste_profile.generate_taste_profile(titles, likes, call_fn=_call)
        if not prof:
            print(f"[Taste] {user}: LLM 失敗，跳過", flush=True)
            continue
        seeds = await taste_profile.resolve_artist_seeds(
            prof.get("adjacent_artists", []), client=yt)
        prof["seed_video_ids"] = seeds
        taste_profile.write_profile(_CACHE, user, prof)
        done += 1
        print(f"[Taste] ✅ {user}: {len(prof.get('adjacent_artists', []))} 鄰近歌手 → "
              f"{len(seeds)} seed / avoid {prof.get('avoid_artists', [])}", flush=True)
    print(f"[Taste] 完成 {done} 位 → {_CACHE}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
