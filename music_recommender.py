"""🎵 自動推薦候選池 builder（純函式）。

點歌佇列空後的自動推薦原本把「變化」交給便宜 LLM → 重複度高。這裡把「變化來源」
與「團體聚合」移到確定性 Python：依在場成員的 MusicMemory 產生三條 lane 的候選，
voice_controller 再在 top-N 做加權隨機抽樣，LLM 只負責把選定錨點 cover 化。

三條 lane：
  - group_resonance：≥2 位在場者都在某歌的 connections（跨人共鳴）→ 直接重播
  - long_tail      ：在場者點過但久沒播（> LONG_TAIL_DAYS）→ 直接重播（重新發現）
  - spotlight      ：輪流聚焦一位在場者的常點歌 → 交給 LLM 推薦 cover 版本

Phase 1 M3 新增：
  - vibe_filter param：用 mood label 對候選做 soft re-rank（boost 命中 feelings 的歌）
  - pick_candidates() ：一次抽 k 首（不重複），給 autopilot 9-pick-3 用
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


def is_already_recommended(title: str, recent_titles: list[str]) -> bool:
    """yt-dlp 解析後的二次門：解析結果是否命中 recent_recommendations ring。

    pool 內部 exclude 只擋住 anchor；spotlight lane 的 LLM coverify 會把 anchor 改寫
    成另一首歌，_resolve_yt_query 拿回的 raw title 可能仍命中 ring（同名熱門原版）。
    本 helper 用同 build_recommendation_pool 的 normalize 規則比對。
    """
    if not title or not recent_titles:
        return False
    return normalize_title(title) in {normalize_title(t) for t in recent_titles}


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


# Phase 1 M3: vibe-aware soft re-rank
# Mood → feelings keyword map（與 music_memory.reactions.feelings 對齊）
# v1 用簡單字串包含、v2 可改 embedding similarity
_MOOD_FEELING_KEYWORDS: dict[str, tuple[str, ...]] = {
    "放鬆": ("chill", "抒情", "夜晚", "安靜", "舒服", "輕鬆", "睡前", "lo-fi", "lofi"),
    "興奮": ("high", "energy", "熱絡", "嗨", "派對", "party", "炸", "燃", "嗨翻"),
    "低落": ("低落", "傷感", "失戀", "孤獨", "難過", "sad", "depressing", "憂"),
    "分歧": (),  # 沒有特定 feeling 關鍵字；改成 boost group_resonance lane（見 _vibe_boost）
}

VIBE_BOOST_PER_FEELING_HIT = 20.0
VIBE_BOOST_GROUP_RESONANCE_ON_SPLIT = 25.0  # mood=分歧 時 group_resonance lane 加分


def _song_feelings_text(song: dict) -> str:
    """把一首歌所有 requester 的 feelings 拼成一個字串（lowercase）做 keyword 比對。"""
    reactions = song.get("reactions", {}) or {}
    blobs: list[str] = []
    for spk_reactions in reactions.values():
        if isinstance(spk_reactions, dict):
            blobs.extend(spk_reactions.get("feelings", []) or [])
    return " ".join(str(x) for x in blobs).lower()


def _vibe_boost(song: dict, lane: str, vibe_filter: dict | None) -> float:
    """根據 vibe_filter 對 (song, lane) 算 soft boost score。"""
    if not vibe_filter:
        return 0.0
    mood = vibe_filter.get("mood")
    if not mood or mood not in _MOOD_FEELING_KEYWORDS:
        return 0.0

    boost = 0.0
    # 分歧：直接 boost group_resonance lane（中介曲、減衝突）
    if mood == "分歧" and lane == "group_resonance":
        boost += VIBE_BOOST_GROUP_RESONANCE_ON_SPLIT

    # 其他 mood：boost 命中 feelings keyword 的歌
    keywords = _MOOD_FEELING_KEYWORDS.get(mood, ())
    if keywords:
        feelings_blob = _song_feelings_text(song)
        if feelings_blob:
            hit = sum(1 for kw in keywords if kw in feelings_blob)
            boost += hit * VIBE_BOOST_PER_FEELING_HIT

    return boost


def build_recommendation_pool(
    *,
    members: list[str],
    songs: dict,
    exclude_titles: list[str],
    now: float,
    spotlight_member: str | None = None,
    vibe_filter: dict | None = None,
) -> list[Candidate]:
    """產生依分數排序（高→低）的候選清單。純函式，不做 I/O。

    members: 當前在場成員 display_name。
    songs:   music_memory 的 _data["songs"] 結構。
    exclude: 不可推薦的標題（會正規化後比對）。
    spotlight_member: 本次輪到聚焦的成員（None → 不產 spotlight 候選）。
    vibe_filter (Phase 1 M3): 可選 dict {mood, topic, min_score}。
      - mood 命中歌曲 feelings keyword → +VIBE_BOOST_PER_FEELING_HIT / hit
      - mood=分歧 → group_resonance lane +VIBE_BOOST_GROUP_RESONANCE_ON_SPLIT
      - min_score：score 低於此值的 candidate 過濾掉
      - vibe_filter=None → 完全 backward-compatible，行為不變
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
            base = 100.0 + 10.0 * len(resonant)
            score = base + _vibe_boost(song, "group_resonance", vibe_filter)
            _offer(Candidate(title, artist, "group_resonance", "direct", None, score))

        # Lane 3: long_tail — 在場者點過 + 久沒播
        if member_set & set(requesters):
            age_days = (now - _last_play_ts(song)) / 86400.0
            if age_days > LONG_TAIL_DAYS:
                base = 40.0 + min(age_days, 30.0)
                score = base + _vibe_boost(song, "long_tail", vibe_filter)
                _offer(Candidate(title, artist, "long_tail", "direct", None, score))

    # Lane 2: spotlight — 聚焦成員的常點歌（mode=cover）
    if spotlight_member:
        spot_songs = sorted(
            (s for s in songs.values() if spotlight_member in s.get("requesters", {})),
            key=lambda s: s["requesters"][spotlight_member],
            reverse=True,
        )
        for s in spot_songs[:3]:
            base = 60.0 + float(s["requesters"][spotlight_member])
            score = base + _vibe_boost(s, "spotlight", vibe_filter)
            _offer(Candidate(s.get("title", ""), s.get("uploader", ""), "spotlight",
                             "cover", spotlight_member, score))

    # Sort + min_score filter (vibe_filter 提供時)
    result = sorted(best.values(), key=lambda c: c.score, reverse=True)
    if vibe_filter and "min_score" in vibe_filter:
        result = [c for c in result if c.score >= vibe_filter["min_score"]]
    return result


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


def pick_candidates(
    pool: list[Candidate],
    *,
    k: int = 3,
    top_n: int = 9,
    rng: random.Random | None = None,
) -> list[Candidate]:
    """Phase 1 M3: 一次抽 k 首（不重複）給 autopilot round。

    Top-N 候選做 weighted-random-without-replacement 抽 k 個。
    若 pool 不足 k 首則回有多少回多少（不報錯，autopilot 視情況決定要不要降級）。
    """
    if not pool:
        return []
    top = pool[:top_n]
    r = rng or random
    if len(top) <= k:
        return list(top)

    # Weighted sample without replacement
    remaining = list(top)
    weights = [max(c.score, 0.1) for c in remaining]
    result: list[Candidate] = []
    for _ in range(k):
        if not remaining:
            break
        idx = r.choices(range(len(remaining)), weights=weights, k=1)[0]
        result.append(remaining.pop(idx))
        weights.pop(idx)
    return result
