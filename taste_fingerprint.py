"""口味指紋（deterministic）：從 music_memory 真人點播統計群組/個人口味摘要。

用途：
  - 每週 review 觀測口味與**漂移**（核心藝人新進/掉出、語言比例變化）。
  - 給未來「錨定式驚喜」當**地板**（語言 / 核心藝人鄰域）。

與 taste_profile.py（LLM 鄰近歌手 seed）互補：這支純統計、無 IO/無 LLM、可測。
2026-06-15 建立（見 [[project_infinite_autopilot_tiers]] review/explore 迴圈）。
"""
from __future__ import annotations

import datetime
import re
from collections import Counter


def _is_human(requester: str) -> bool:
    """真人點播者（排除 'Marvin推薦（為X）' 等自薦）。"""
    return bool(requester) and "Marvin" not in requester and "推薦" not in requester


def _human_plays(song: dict) -> int:
    return sum(c for r, c in (song.get("requesters") or {}).items() if _is_human(r))


def artist_of(title: str) -> str:
    """從標題抽藝人段（首個分隔符前），去英文副名；抽不到回 ''。"""
    t = (title or "").strip()
    if not t:
        return ""
    head = re.split(r"[-–—【《\[(|]", t, maxsplit=1)[0].strip()
    # 中文藝人去英文副名（「周杰倫 Jay Chou」→「周杰倫」）；純英文名整段保留。
    if re.search(r"[一-鿿]", head):
        zh = re.sub(r"\s+[A-Za-z].*$", "", head).strip()
        head = zh or head
    return head[:30]


def classify_language(title: str) -> str:
    """粗分語言：CJK≥3 → 華語；英文字母>3 → 英文；其餘 → 其他。"""
    t = title or ""
    cjk = len(re.findall(r"[一-鿿]", t))
    en = len(re.findall(r"[A-Za-z]", t))
    if cjk >= 3:
        return "華語"
    if en > 3:
        return "英文"
    return "其他"


def compute_taste_fingerprint(songs: dict, *, top_n: int = 15,
                              today: str | None = None) -> dict:
    """從 songs dict（music_memory['songs']）算出口味指紋。"""
    total = 0
    distinct = 0
    artist_cnt: Counter = Counter()
    lang_cnt: Counter = Counter()
    per_user_artist: dict[str, Counter] = {}
    per_user_total: Counter = Counter()

    for song in (songs or {}).values():
        h = _human_plays(song)
        if h <= 0:
            continue
        distinct += 1
        total += h
        title = song.get("title", "")
        a = artist_of(title)
        if a:
            artist_cnt[a] += h
        lang_cnt[classify_language(title)] += h
        for r, c in (song.get("requesters") or {}).items():
            if _is_human(r):
                per_user_artist.setdefault(r, Counter())[artist_of(title)] += c
                per_user_total[r] += c

    language = {k: round(v / total, 3) for k, v in lang_cnt.most_common()} if total else {}
    per_user = {
        u: {
            "requests": per_user_total[u],
            "core_artists": [[a, c] for a, c in per_user_artist[u].most_common(5) if a],
        }
        for u, _ in per_user_total.most_common()
    }
    return {
        "total_human_requests": total,
        "distinct_songs": distinct,
        "language": language,
        "core_artists": [[a, c] for a, c in artist_cnt.most_common(top_n)],
        "per_user": per_user,
        "updated": today or datetime.date.today().isoformat(),
    }


def diff_fingerprints(old: dict, new: dict) -> dict:
    """跟上一份指紋比漂移：核心藝人新進/掉出 + 語言比例變化（門檻 0.05）。"""
    old_a = {a for a, _ in (old or {}).get("core_artists", [])}
    new_a = {a for a, _ in (new or {}).get("core_artists", [])}
    lang_old = (old or {}).get("language", {})
    lang_new = (new or {}).get("language", {})
    lang_shift = {
        k: round(lang_new.get(k, 0) - lang_old.get(k, 0), 3)
        for k in set(lang_old) | set(lang_new)
        if abs(lang_new.get(k, 0) - lang_old.get(k, 0)) >= 0.05
    }
    return {
        "new_core_artists": sorted(new_a - old_a),
        "dropped_core_artists": sorted(old_a - new_a),
        "language_shift": lang_shift,
    }
