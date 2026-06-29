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
    本 helper 用同 normalize_title 的規則比對。
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
    direct_url: str = ""         # T2 discovery：自帶 YouTube URL → enqueue 時直解不搜尋


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


def build_member_pools(
    *,
    members: list[str],
    songs: dict,
    exclude_titles: list[str],
    now: float,
    vibe_filter: dict | None = None,
) -> dict[str, list[Candidate]]:
    """對每個在場成員各自產候選池 dict[member -> 依分數排序的 [Candidate]]。

    一首歌對成員 M 是候選 iff M 在該歌 requesters。沿用三條 lane 計分，
    但每個 Candidate.target_member 一律填 M（擁有者明確）：
      - group_resonance（M 也在 ≥2 在場共鳴名單）→ direct
      - long_tail（M 點過且久沒播）→ direct
      - spotlight（M 的常點 top-3）→ cover

    純函式、不做 I/O。給 assign_unique_owners 做跨使用者去重的上游。
    """
    member_set = set(members)
    exclude_norm = {normalize_title(t) for t in exclude_titles}
    pools: dict[str, dict[str, Candidate]] = {m: {} for m in members}

    def _offer(member: str, cand: Candidate) -> None:
        nt = normalize_title(cand.anchor_title)
        if nt in exclude_norm:
            return
        best = pools[member]
        cur = best.get(nt)
        if cur is None or cand.score > cur.score:
            best[nt] = cand

    # 每位成員的常點 top-3（spotlight lane，mode=cover）— 對齊原 spotlight 行為，避免
    # 把每首點過的歌都灌成 cover 候選。
    top3: dict[str, set[str]] = {}
    for m in member_set:
        m_songs = sorted(
            (s for s in songs.values() if m in s.get("requesters", {})),
            key=lambda s: s["requesters"][m], reverse=True,
        )
        top3[m] = {s.get("title", "") for s in m_songs[:3]}

    for song in songs.values():
        title = song.get("title", "")
        if not title:
            continue
        requesters = song.get("requesters", {})
        artist = song.get("uploader", "")
        resonant = member_set & set(song.get("connections", []))
        age_days = (now - _last_play_ts(song)) / 86400.0

        for m in member_set & set(requesters):
            # Lane 1: group_resonance（M 也在共鳴名單且 ≥2 在場共鳴）
            if len(resonant) >= GROUP_RESONANCE_MIN and m in resonant:
                base = 100.0 + 10.0 * len(resonant)
                _offer(m, Candidate(title, artist, "group_resonance", "direct", m,
                                    base + _vibe_boost(song, "group_resonance", vibe_filter)))
            # Lane 3: long_tail（M 點過 + 久沒播）
            if age_days > LONG_TAIL_DAYS:
                base = 40.0 + min(age_days, 30.0)
                _offer(m, Candidate(title, artist, "long_tail", "direct", m,
                                    base + _vibe_boost(song, "long_tail", vibe_filter)))
            # Lane 2: spotlight（M 的常點 top-3）
            if title in top3.get(m, set()):
                base = 60.0 + float(requesters[m])
                _offer(m, Candidate(title, artist, "spotlight", "cover", m,
                                    base + _vibe_boost(song, "spotlight", vibe_filter)))

    result: dict[str, list[Candidate]] = {}
    for m, best in pools.items():
        cands = sorted(best.values(), key=lambda c: c.score, reverse=True)
        if vibe_filter and "min_score" in vibe_filter:
            cands = [c for c in cands if c.score >= vibe_filter["min_score"]]
        result[m] = cands
    return result


def assign_unique_owners(
    member_pools: dict[str, list[Candidate]],
    *,
    rotation_order: list[str] | None = None,
) -> dict[str, list[Candidate]]:
    """跨使用者去重：每首歌（normalize 後）只歸一個成員，回傳去重後的 per-member 池。

    一首歌被多人列為候選（＝大家都愛的高分候選）時，靠 round-robin 平手代表分配，盡量
    讓每個在場者都被代表到，不讓單人通吃；只一人候選的歌維持歸該人。各成員池內保留原排序。

    contested 計數只計「被搶過的歌」，所以是 contested 之間的輪流，與某人有多少獨享歌無關。
    平手序：contested 已分配數少者優先 → rotation_order 在前者 → 分數高者。
    """
    order = rotation_order or list(member_pools.keys())
    order_idx = {m: i for i, m in enumerate(order)}

    offers: dict[str, list[tuple[str, Candidate]]] = {}
    for m, cands in member_pools.items():
        for c in cands:
            offers.setdefault(normalize_title(c.anchor_title), []).append((m, c))

    contested_count = {m: 0 for m in member_pools}
    winner: dict[str, str] = {}

    def _title_key(nt: str) -> tuple[float, str]:
        return (-max(o[1].score for o in offers[nt]), nt)

    for nt in sorted(offers, key=_title_key):
        contenders = offers[nt]
        if len(contenders) == 1:
            winner[nt] = contenders[0][0]
            continue
        m = min(
            contenders,
            key=lambda oc: (contested_count[oc[0]], order_idx.get(oc[0], 1 << 30), -oc[1].score),
        )[0]
        winner[nt] = m
        contested_count[m] += 1

    result: dict[str, list[Candidate]] = {m: [] for m in member_pools}
    for m, cands in member_pools.items():
        for c in cands:
            if winner.get(normalize_title(c.anchor_title)) == m:
                result[m].append(c)
    return result


def is_low_quality_version(cand: "Candidate") -> bool:
    """cover / 現場版 = 品質與口味雜訊：自動推薦 cover 佔 11% vs 真人只 3%、live 也 2 倍，
    humans 明顯避開。spotlight lane 的 mode='cover' 一律算；其餘看標題。"""
    from track_quality import looks_like_cover, looks_like_live
    if cand.mode == "cover":
        return True
    t = cand.anchor_title or ""
    return looks_like_cover(t) or looks_like_live(t)


def demote_low_quality_versions(cands: list["Candidate"]) -> list["Candidate"]:
    """穩定重排：官方/錄音室版優先、cover/現場版沉到隊尾。**不丟棄**——沒有更好的候選時
    仍會播（不枯竭、不停播）；有更好的就先填滿 round → 自動推薦品質貼近真人口味。"""
    preferred = [c for c in cands if not is_low_quality_version(c)]
    demoted = [c for c in cands if is_low_quality_version(c)]
    return preferred + demoted


def ring_titles_for(played_title: str, mode: str, anchor_title: str) -> list[str]:
    """推薦一首後，該寫進 novelty ring 的標題清單。

    direct lane：只記實際播放的標題。
    cover lane（spotlight）：連 anchor 原曲一起記 — 否則 ring 只擋住 cover 後的
    標題，anchor 下輪仍可被選中再 cover 成另一個版本，造成「同一首歌反覆出現」的
    重複感（spotlight 重複根因）。
    """
    titles = [played_title] if played_title else []
    if mode == "cover" and anchor_title and anchor_title != played_title:
        titles.append(anchor_title)
    return titles


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
