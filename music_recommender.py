"""🎵 自動推薦候選池 builder（純函式）。

點歌佇列空後的自動推薦原本把「變化」交給便宜 LLM → 重複度高。這裡把「變化來源」
與「團體聚合」移到確定性 Python：依在場成員的 MusicMemory 產生三條 lane 的候選，
voice_controller 再在 top-N 做加權隨機抽樣，LLM 只負責把選定錨點 cover 化。

三條 lane：
  - group_resonance：≥2 位在場者都在某歌的 connections（跨人共鳴）→ 直接重播
  - long_tail      ：在場者點過但久沒播（> LONG_TAIL_DAYS）→ 直接重播（重新發現）
  - spotlight      ：輪流聚焦一位在場者的常點歌 → 交給 LLM 推薦 cover 版本
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass

# 長尾門檻：最後一次播放超過這天數才算「久沒播」
LONG_TAIL_DAYS = 7.0
# 群體共鳴需要的最少在場共鳴人數
GROUP_RESONANCE_MIN = 2

# 標題正規化：去掉版本／合作等變體後綴，讓「晴天」與「晴天 (cover)」視為同一首
_VARIANT_RE = re.compile(
    r"\s*[\(（\[].*?(cover|live|現場|翻唱|remix|acoustic|版).*?[\)）\]]"
    r"|\s*feat\.?.*$|\s*ft\.?.*$",
    re.IGNORECASE,
)


def normalize_title(title: str) -> str:
    """正規化標題供 dedup / exclude 比對（去變體後綴、去空白、casefold）。"""
    t = _VARIANT_RE.sub("", title or "")
    return re.sub(r"\s+", "", t).casefold()


@dataclass
class Candidate:
    anchor_title: str
    anchor_artist: str          # uploader / 原藝人；spotlight cover 用
    lane: str                   # group_resonance | long_tail | spotlight
    mode: str                   # direct（重播）| cover（交給 LLM cover 化）
    target_member: str | None   # spotlight 聚焦對象
    score: float


def _last_play_ts(song: dict) -> float:
    return max((p.get("ts", 0.0) for p in song.get("plays", [])), default=0.0)


def build_recommendation_pool(
    *,
    members: list[str],
    songs: dict,
    exclude_titles: list[str],
    now: float,
    spotlight_member: str | None = None,
) -> list[Candidate]:
    """產生依分數排序（高→低）的候選清單。純函式，不做 I/O。

    members: 當前在場成員 display_name。
    songs:   music_memory 的 _data["songs"] 結構。
    exclude: 不可推薦的標題（會正規化後比對）。
    spotlight_member: 本次輪到聚焦的成員（None → 不產 spotlight 候選）。
    """
    member_set = set(members)
    exclude_norm = {normalize_title(t) for t in exclude_titles}
    best: dict[str, Candidate] = {}  # norm_title → 最高分候選（跨 lane dedup）

    def _offer(cand: Candidate) -> None:
        nt = normalize_title(cand.anchor_title)
        if nt in exclude_norm:
            return
        cur = best.get(nt)
        if cur is None or cand.score > cur.score:
            best[nt] = cand

    for song in songs.values():
        title = song.get("title", "")
        if not title:
            continue
        requesters = song.get("requesters", {})
        artist = song.get("uploader", "")

        # Lane 1: group_resonance — ≥2 在場者共鳴
        resonant = member_set & set(song.get("connections", []))
        if len(resonant) >= GROUP_RESONANCE_MIN:
            _offer(Candidate(title, artist, "group_resonance", "direct",
                             None, 100.0 + 10.0 * len(resonant)))

        # Lane 3: long_tail — 在場者點過 + 久沒播
        if member_set & set(requesters):
            age_days = (now - _last_play_ts(song)) / 86400.0
            if age_days > LONG_TAIL_DAYS:
                _offer(Candidate(title, artist, "long_tail", "direct",
                                 None, 40.0 + min(age_days, 30.0)))

    # Lane 2: spotlight — 聚焦成員的常點歌（mode=cover）
    if spotlight_member:
        spot_songs = sorted(
            (s for s in songs.values() if spotlight_member in s.get("requesters", {})),
            key=lambda s: s["requesters"][spotlight_member],
            reverse=True,
        )
        for s in spot_songs[:3]:
            _offer(Candidate(s.get("title", ""), s.get("uploader", ""), "spotlight",
                             "cover", spotlight_member,
                             60.0 + float(s["requesters"][spotlight_member])))

    return sorted(best.values(), key=lambda c: c.score, reverse=True)


def pick_candidate(
    pool: list[Candidate],
    *,
    rng: random.Random | None = None,
    top_n: int = 5,
) -> Candidate | None:
    """從候選池 top-N 做分數加權隨機抽樣 → 變化來源（避免每次都選最高分那首）。"""
    if not pool:
        return None
    top = pool[:top_n]
    r = rng or random
    return r.choices(top, weights=[max(c.score, 0.1) for c in top], k=1)[0]
