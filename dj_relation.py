"""DJ 口白的「這首歌跟在場的人有什麼關係」——只用播放紀錄算，算不出具體關係就回 None（10/18 使用者定）。"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Callable, Collection

LISTEN_MIN = 3   # 「常聽」門檻：某人聽這首/這位歌手 ≥3 次

# 10/18 使用者：「我們大多數都是常常聽而已」——autopilot 挑給 X 的播放（Marvin推薦（為X））也算 X 聽過；
# 「點給大家」等其他 Marvin 掛名不算任何人。
_FOR_WHO = re.compile(r"Marvin推薦（為(.+)）")


def _listener(name: str) -> str:
    """requesters 的 key → 聽的人；不屬於任何人回 ""。"""
    m = _FOR_WHO.fullmatch(name or "")
    if m:
        return m.group(1)
    return "" if "Marvin" in (name or "") else (name or "")


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
    """在 songs 裡找「同一首歌」的所有 entry，把 requesters 加總成 {人名: 次數}。

    無法確認藝人一致（任一邊藝人正規化後為空）就不算，說錯不如沒說（10/23 review）。
    """
    norm_title = _norm(title)
    norm_artist = _norm(artist)
    if not norm_title or not norm_artist:
        return {}
    out: dict[str, int] = {}
    for entry in songs.values():
        if not isinstance(entry, dict):
            continue
        e_title, e_artist = display(entry)
        if _norm(e_title) != norm_title:
            continue
        norm_e_artist = _norm(e_artist)
        if norm_e_artist != norm_artist:
            continue
        for name, count in (entry.get("requesters") or {}).items():
            name = _listener(name)
            if not name:
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
            name = _listener(name)
            if not name:
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
    from song_name_clean import speakable_artist, speakable_title

    humans = {m for m in (present or ()) if m and "Marvin" not in m}
    if not humans:
        return None
    if speakable_title(title) is None:
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
        if _norm(a_t) and _norm(a_t) != _norm(title) and speakable_title(a_t):
            ac_counts = song_counts(songs, a_t, a_a, display=display)
            ac_ranked = _ranked(ac_counts, humans, prefer=prefer)
            if ac_ranked and ac_ranked[0][1] >= LISTEN_MIN:
                return Relation("anchor_song", ac_ranked[0][0], title, artist, anchor=a_t)

    if speakable_artist(artist):
        artc = artist_counts(songs, artist, display=display)
        art_ranked = _ranked(artc, humans, prefer=prefer)
        if art_ranked and art_ranked[0][1] >= LISTEN_MIN:
            return Relation("same_artist", art_ranked[0][0], title, artist)

    if personal_user and personal_user in humans:
        return Relation("personal_list", personal_user, title, artist)

    return None


def render_relation(rel: Relation, templates: dict[str, list[str]], *, recent: Collection[str] = (), rng=random) -> tuple[str, str]:
    from song_name_clean import speakable_artist, speakable_title

    pool = templates[rel.kind]
    fresh = [t for t in pool if t not in recent] or list(pool)
    tmpl = rng.choice(fresh)
    # build_relation 已確認念得出口；這裡取剝掉尾端註記的版本（「一念(電視劇《逐玉》插曲)」→「一念」）
    title = speakable_title(rel.title) or rel.title
    anchor = speakable_title(rel.anchor) or rel.anchor
    artist = speakable_artist(rel.artist)
    song = f"{artist}的《{title}》" if artist else f"《{title}》"
    text = tmpl.format(who=rel.who, title=title, artist=artist, anchor=anchor, song=song)
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
