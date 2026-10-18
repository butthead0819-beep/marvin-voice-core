"""DJ 口白的「這首歌跟在場的人有什麼關係」——只用播放紀錄算，算不出具體關係就回 None（10/18 使用者定）。"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Callable, Collection

LISTEN_MIN = 3   # 「常聽」門檻：某人點這首/這位歌手 ≥3 次


@dataclass(frozen=True)
class Relation:
    kind: str     # listens_song | group_listened | anchor_song | same_artist | personal_list
    who: str      # 人名；group_listened 為 "A 跟 B"（最多兩人，依次數高到低）
    title: str
    artist: str
    anchor: str = ""   # anchor_song 才有：錨點歌的乾淨歌名


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "").casefold()


def song_counts(songs: dict, title: str, artist: str, *, display: Callable[[dict], tuple[str, str]]) -> dict[str, int]:
    """在 songs 裡找「同一首歌」的所有 entry，把 requesters 加總成 {人名: 次數}。"""
    if not title:
        return {}
    norm_title = _norm(title)
    norm_artist = _norm(artist)
    out: dict[str, int] = {}
    for entry in songs.values():
        if not isinstance(entry, dict):
            continue
        e_title, e_artist = display(entry)
        if _norm(e_title) != norm_title:
            continue
        norm_e_artist = _norm(e_artist)
        if norm_artist and norm_e_artist and norm_artist != norm_e_artist:
            continue
        for name, count in (entry.get("requesters") or {}).items():
            if "Marvin" in name:
                continue
            if not isinstance(count, int) or count <= 0:
                continue
            out[name] = out.get(name, 0) + count
    return out


def artist_counts(songs: dict, artist: str, *, display: Callable[[dict], tuple[str, str]]) -> dict[str, int]:
    """在 songs 裡找「同一位歌手」的所有 entry，把 requesters 加總成 {人名: 次數}。"""
    if not artist:
        return {}
    norm_artist = _norm(artist)
    out: dict[str, int] = {}
    for entry in songs.values():
        if not isinstance(entry, dict):
            continue
        _e_title, e_artist = display(entry)
        if _norm(e_artist) != norm_artist:
            continue
        for name, count in (entry.get("requesters") or {}).items():
            if "Marvin" in name:
                continue
            if not isinstance(count, int) or count <= 0:
                continue
            out[name] = out.get(name, 0) + count
    return out


def _ranked(counts: dict[str, int], humans: set[str], *, prefer: Collection[str] = ()) -> list[tuple[str, int]]:
    rows = [(name, count) for name, count in counts.items() if name in humans]
    rows.sort(key=lambda row: (row[1], row[0] in prefer), reverse=True)
    return rows


def build_relation(
    *, songs: dict, title: str, artist: str, present,
    display: Callable[[dict], tuple[str, str]],
    spotlight: str = "", personal_user: str = "", anchor_title: str = "",
) -> Relation | None:
    humans = {m for m in (present or ()) if m and "Marvin" not in m}
    if not humans:
        return None
    prefer = {n for n in (spotlight, personal_user) if n}

    sc = song_counts(songs, title, artist, display=display)
    ranked = _ranked(sc, humans, prefer=prefer)
    if ranked and ranked[0][1] >= LISTEN_MIN:
        return Relation("listens_song", ranked[0][0], title, artist)

    group = [row for row in ranked if row[1] >= 1]
    if len(group) >= 2 and sum(count for _n, count in group) >= LISTEN_MIN:
        who = " 跟 ".join(name for name, _c in group[:2])
        return Relation("group_listened", who, title, artist)

    if anchor_title:
        a_t, a_a = display({"title": anchor_title})
        if _norm(a_t) and _norm(a_t) != _norm(title):
            ac_counts = song_counts(songs, a_t, a_a, display=display)
            ac_ranked = _ranked(ac_counts, humans, prefer=prefer)
            if ac_ranked and ac_ranked[0][1] >= LISTEN_MIN:
                return Relation("anchor_song", ac_ranked[0][0], title, artist, anchor=a_t)

    artc = artist_counts(songs, artist, display=display)
    art_ranked = _ranked(artc, humans, prefer=prefer)
    if art_ranked and art_ranked[0][1] >= LISTEN_MIN:
        return Relation("same_artist", art_ranked[0][0], title, artist)

    if personal_user and personal_user in humans:
        return Relation("personal_list", personal_user, title, artist)

    return None


def render_relation(rel: Relation, templates: dict[str, list[str]], *, recent: Collection[str] = (), rng=random) -> tuple[str, str]:
    pool = templates[rel.kind]
    fresh = [t for t in pool if t not in recent] or list(pool)
    tmpl = rng.choice(fresh)
    song = f"{rel.artist}的《{rel.title}》" if rel.artist else f"《{rel.title}》"
    text = tmpl.format(who=rel.who, title=rel.title, artist=rel.artist, anchor=rel.anchor, song=song)
    return text, tmpl


def valid_relation_tail(tail: str, names: Collection[str]) -> bool:
    """LLM 補的那一句是否可用。"""
    t = (tail or "").strip()
    if not (4 <= len(t) <= 40):
        return False
    for name in names:
        if name and name in t:
            return False
    if re.search(r"常聽|愛聽|聽過|你們|點的|點過|又點|常點", t):
        return False
    stripped = re.sub(r"[『「《].*?[』」》]", "", t)
    if "我" in stripped:
        return False
    from dj_prompt_builder import FORBIDDEN_DJ_PHRASES
    for fb in FORBIDDEN_DJ_PHRASES:
        if fb in t:
            return False
    return True
