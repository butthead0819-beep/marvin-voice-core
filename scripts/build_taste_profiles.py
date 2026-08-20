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
_MUSIC_GENRE_HINT = ("音樂", "唱歌", "歌曲", "歌手", "演唱會", "專輯", "MV", "神曲", "副歌",
                     "旋律", "抒情", "搖滾", "嘻哈", "饒舌", "民謠", "爵士", "電音", "金曲", "R&B")


def _load_env():
    f = BASE / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _music_related_likes(likes: list[str], core_artists: list[str]) -> list[str]:
    """興趣標籤裡跟音樂相關的部分：命中音樂類型多字詞彙，或提到使用者自己真的
    聽過的藝人（deterministic 指紋 core_artists 交叉比對）。

    舊版用姓氏單字 hint（張/周/林）誤殺率高——任何提到「周末」「張三」的興趣都
    會被誤收；改用多字詞彙降低這類假陽性，並用真實聽過的藝人名取代猜測式的
    姓氏字元，兼顧「漏收」（原本沒有 hint 到的藝人名，如「五月天」不含任何舊
    hint 字）。
    """
    core = [c.strip() for c in core_artists if c and c.strip()]
    out = []
    for l in likes:
        if not l:
            continue
        if any(h in l for h in _MUSIC_GENRE_HINT) or any(c in l for c in core):
            out.append(l)
    return out


def _gather(user: str, mm: dict, sk: dict, core_artists: list[str] | None = None) -> tuple[list[str], list[str]]:
    """回 (該使用者真人點過/聽過的歌名, 音樂相關興趣標籤)。

    music_memory 的歌依該使用者點播次數（`requesters` count）降冪排序——愛播的歌
    才是真正代表品味的樣本，聽過 1 次跟愛播 10 次不該被同等對待。song_history
    沒有次數只有標題，當補充訊號接在後面，依最近性排序（`add_song_history` 只
    append 到尾巴，故 reversed 讓最近聽的排前面）。dedup 保留先出現（權重較高
    或較新）的位置。
    """
    weighted: list[tuple[str, int]] = []
    for _url, s in (mm.get("songs") or {}).items():
        reqs = s.get("requesters", {}) or {}
        count = sum(c for r, c in reqs.items() if user in r and "Marvin" not in r and "推薦" not in r)
        if count > 0:
            t = s.get("title", "")
            if t:
                weighted.append((t, count))
    weighted.sort(key=lambda x: x[1], reverse=True)
    titles = [t for t, _c in weighted]
    p = (sk.get("players") or {}).get(user, {})
    titles += [t for t in reversed(p.get("song_history") or []) if t]
    titles = list(dict.fromkeys(titles))[:_MAX_SONGS]
    likes = _music_related_likes(p.get("likes") or [], core_artists or [])
    return titles, likes


def _skipped_titles(user: str, mm: dict) -> list[str]:
    """該使用者最新一筆 feedback 為 skipped 的歌名（負向訊號，餵進 LLM 讓 avoid_artists
    有憑有據）。複製 `music_memory.MusicMemory.get_skipped_titles` 的 latest-wins 邏輯，
    但吃原始 dict 不建立 MusicMemory 實例——這支批次腳本只讀 music_memory.json，
    不該觸發 MusicMemory.__init__ 的 key migration/落地存檔 side effect。
    """
    latest: dict[str, str] = {}
    for f in mm.get("recommendations", {}).get(user, {}).get("feedback", []):
        t = f.get("title")
        if t and f.get("result"):
            latest[t] = f["result"]
    return [t for t, r in latest.items() if r == "skipped"]


async def main():
    _load_env()
    import taste_fingerprint
    import taste_profile
    from llm_pool import call_paid_review
    from ytmusicapi import YTMusic

    mm = json.loads((BASE / "music_memory.json").read_text(encoding="utf-8"))
    sk = json.loads((BASE / "suki_memory.json").read_text(encoding="utf-8"))
    fp = taste_fingerprint.compute_taste_fingerprint(mm.get("songs") or {})

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
        core_artists = [a for a, _c in fp.get("per_user", {}).get(user, {}).get("core_artists", [])]
        titles, likes = _gather(user, mm, sk, core_artists=core_artists)
        if len(titles) < _MIN_SONGS:
            print(f"[Taste] {user}: 歌 {len(titles)} < {_MIN_SONGS}，跳過", flush=True)
            continue
        skipped = _skipped_titles(user, mm)
        prof = await taste_profile.generate_taste_profile(titles, likes, call_fn=_call, skipped=skipped)
        if not prof:
            print(f"[Taste] {user}: LLM 失敗，跳過", flush=True)
            continue
        prof = taste_profile.sanitize_profile(prof, core_artists)
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
