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

import json
import logging
import os
import random
import re
from dataclasses import dataclass

from music_memory import extract_video_id

logger = logging.getLogger(__name__)

# 長尾門檻：最後一次播放超過這天數才算「久沒播」
LONG_TAIL_DAYS = 7.0
# 群體共鳴需要的最少在場共鳴人數
GROUP_RESONANCE_MIN = 2

# BPM soft re-rank（見 bpm_estimate.py 取樣落地）：容忍帶內線性衰減，超出不扣分只是 0。
BPM_BOOST_MAX = 15.0
BPM_TOLERANCE = 30.0

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


def find_recent_same_song(
    title: str,
    recent_titles: list[str],
    threshold: float = 90.0,
    min_core_len: int = 4,
) -> str | None:
    """回傳 recent_titles 中與 title「同歌不同上傳」的第一首（否則 None）。

    補 video-id dedup 的漏：同一首歌在 YouTube 有多個上傳（官方 MV vs 純歌名版）→
    video-id 不同 → video-id 排除認不出；normalize_title 又因藝人前綴 /「(Official
    …)」後綴使長短標題不 exact 相等 → title ring 也漏 → autopilot 重推同歌。

    正規化後用 rapidfuzz partial_ratio 做「短標題含於長標題」比對（子字串對齊）。
    min_core_len 守門：過短核心（<4，如「情歌」「小半」）不做 fuzzy，避免子字串
    誤殺不同歌——寧可偶爾漏一次也不誤殺（使用者訂）。
    """
    if not title or not recent_titles:
        return None
    nt = normalize_title(title)
    if not nt:
        return None
    from rapidfuzz import fuzz

    for rt in recent_titles:
        nrt = normalize_title(rt)
        if not nrt:
            continue
        if nt == nrt:
            return rt
        shorter, longer = (nt, nrt) if len(nt) <= len(nrt) else (nrt, nt)
        if len(shorter) < min_core_len:
            continue
        if fuzz.partial_ratio(shorter, longer) >= threshold:
            return rt
    return None


@dataclass
class Candidate:
    anchor_title: str
    anchor_artist: str          # uploader / 原藝人；spotlight cover 用
    lane: str                   # group_resonance | long_tail | spotlight
    mode: str                   # direct（重播）| cover（交給 LLM cover 化）
    target_member: str | None   # spotlight 聚焦對象
    score: float
    direct_url: str = ""         # T2 discovery：自帶 YouTube URL → enqueue 時直解不搜尋
    discovery_seed_title: str = ""  # T2 discovery：觸發這首候選的 YT Music radio 種子曲名


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


def _bpm_boost(song: dict, bpm_filter: dict | None) -> float:
    """候選歌 BPM 越接近目前播放歌 → boost 越高（soft re-rank，同 _vibe_boost 設計）。

    bpm_filter={"current_bpm": float, "store": {video_id: {"bpm": float, ...}}}。
    任一沒 BPM 記錄（新歌/還沒被 bpm_estimate 取樣過）或差距超出 BPM_TOLERANCE →
    0，不影響原排序——BPM 資料是漸進覆蓋的，不能讓沒資料的歌被扣分排擠掉。
    """
    if not bpm_filter:
        return 0.0
    current_bpm = bpm_filter.get("current_bpm")
    store = bpm_filter.get("store") or {}
    if not current_bpm or not store:
        return 0.0
    vid = extract_video_id(song.get("webpage_url") or song.get("url") or "")
    if not vid:
        return 0.0
    entry = store.get(vid)
    if not isinstance(entry, dict):
        return 0.0
    bpm = entry.get("bpm")
    if bpm is None:
        return 0.0
    diff = abs(float(bpm) - float(current_bpm))
    if diff >= BPM_TOLERANCE:
        return 0.0
    return BPM_BOOST_MAX * (1.0 - diff / BPM_TOLERANCE)


def build_member_pools(
    *,
    members: list[str],
    songs: dict,
    exclude_titles: list[str],
    now: float,
    vibe_filter: dict | None = None,
    bpm_filter: dict | None = None,
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
        likes = song.get("likes", {}) or {}
        artist = song.get("uploader", "")
        resonant = member_set & set(song.get("connections", []))
        age_days = (now - _last_play_ts(song)) / 86400.0

        bpm_boost = _bpm_boost(song, bpm_filter)

        # Lane: liked（M 按讚→喜好擴散成候選；base 30 次於點播者 lanes 40/60/100）。
        # _offer 保留高分：M 若也點過，requester lane 分數更高會勝出、不被 liked 拉低。
        for m in member_set & set(likes):
            _offer(m, Candidate(title, artist, "liked", "direct", m,
                                30.0 + _vibe_boost(song, "liked", vibe_filter) + bpm_boost))

        for m in member_set & set(requesters):
            # Lane 1: group_resonance（M 也在共鳴名單且 ≥2 在場共鳴）
            if len(resonant) >= GROUP_RESONANCE_MIN and m in resonant:
                base = 100.0 + 10.0 * len(resonant)
                _offer(m, Candidate(title, artist, "group_resonance", "direct", m,
                                    base + _vibe_boost(song, "group_resonance", vibe_filter) + bpm_boost))
            # Lane 3: long_tail（M 點過 + 久沒播）
            if age_days > LONG_TAIL_DAYS:
                base = 40.0 + min(age_days, 30.0)
                _offer(m, Candidate(title, artist, "long_tail", "direct", m,
                                    base + _vibe_boost(song, "long_tail", vibe_filter) + bpm_boost))
            # Lane 2: spotlight（M 的常點 top-3）
            if title in top3.get(m, set()):
                base = 60.0 + float(requesters[m])
                _offer(m, Candidate(title, artist, "spotlight", "cover", m,
                                    base + _vibe_boost(song, "spotlight", vibe_filter) + bpm_boost))

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


# ── Evidence 抽取層（供解釋層槽位填空用；不准 LLM 自由編造事實）──────────────
# 從 song.plays[]/requesters 抽 rediscover 訊號（timestamp/play_count）。
# source_tier 沿用本檔 lane 命名（group_resonance/long_tail/spotlight/liked），
# 不是 project_infinite_autopilot_tiers 的 T1/T2/T3 那套命名——兩者是不同子系統。
@dataclass
class Evidence:
    signal_type: str            # listen | like | adjacent_artist | radio_related
    timestamp: float | None     # 最近一次相關事件的 unix ts（無資料時 None）
    play_count: int
    skip_count: int
    source_tier: str            # 候選來源 lane：group_resonance/long_tail/spotlight/liked/adjacent_artist
    subject: str                # "you"（單一 requester）| "you_all"（群體共同歷史）
    requester: str | None       # subject == "you" 時的具名對象，否則 None
    seed_title: str | None = None  # signal_type=="radio_related" 時：觸發的 YT Music radio 種子曲名


def extract_evidence(song: dict, candidate: "Candidate") -> Evidence | None:
    """從 song.plays[]/requesters/likes 抽 Evidence，供解釋層槽位填空使用。

    候選歌曲只有單一 requester 時才用人名化解釋（subject="you"）；多人點過則
    群體共同歷史用「你們」（subject="you_all"），不猜測在場哪位互動最多。

    fail-open：song 關鍵欄位型別異常（非 dict/list）→ log 記錄被跳過的候選、
    回傳 None，不拋例外——caller 該跳過該候選的解釋顯示，不中斷整段推薦流程。
    """
    title = getattr(candidate, "anchor_title", None)
    try:
        plays = song.get("plays") or []
        if not isinstance(plays, list):
            raise TypeError(f"plays 應為 list，實際 {type(plays)!r}")
        requesters = song.get("requesters") or {}
        if not isinstance(requesters, dict):
            raise TypeError(f"requesters 應為 dict，實際 {type(requesters)!r}")

        target = candidate.target_member
        member_plays = [p for p in plays if isinstance(p, dict) and p.get("by") == target]
        relevant_plays = member_plays or [p for p in plays if isinstance(p, dict)]
        timestamps = [p.get("ts") for p in relevant_plays if isinstance(p.get("ts"), (int, float))]
        timestamp = max(timestamps) if timestamps else None
        play_count = len(member_plays) if member_plays else int(requesters.get(target, 0) or 0)

        active_requesters = [u for u, n in requesters.items() if n]
        if len(active_requesters) <= 1:
            subject, requester = "you", (active_requesters[0] if active_requesters else target)
        else:
            subject, requester = "you_all", None

        signal_type = "like" if candidate.lane == "liked" else "listen"

        return Evidence(
            signal_type=signal_type,
            timestamp=timestamp,
            play_count=play_count,
            skip_count=0,
            source_tier=candidate.lane,
            subject=subject,
            requester=requester,
        )
    except (TypeError, AttributeError, ValueError) as e:
        logger.warning("evidence 抽取失敗，跳過候選 %r：%s", title, e)
        return None


def extract_radio_related_evidence(cand: "Candidate") -> Evidence | None:
    """T2 discovery 候選（lane="discovery"）：從觸發的 YT Music radio 種子曲名產生
    grounded evidence——候選本身沒有播放史（是真的新歌），故不能走 rediscover 路徑；
    這裡的「可查證事實」是「radio API 真的把這首跟種子曲關聯在一起」，不是猜測。

    只有 cand.discovery_seed_title 有值才回 Evidence（T2 建構候選時已把種子曲名帶上，
    見 cogs/music_cog.py `_t2_discovery_candidates`）。無種子曲名（如 T4 冒險發現，
    是 query 搜尋而非種子關聯）→ None，交給其他 evidence 路徑或不顯示解釋。
    """
    seed_title = (getattr(cand, "discovery_seed_title", "") or "").strip()
    if not seed_title:
        return None
    return Evidence(
        signal_type="radio_related",
        timestamp=None,
        play_count=0,
        skip_count=0,
        source_tier=cand.lane,
        subject="you_all",
        requester=None,
        seed_title=seed_title,
    )


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


# ── 情境切換選擇器（rediscover / discover_new，仿 dj_topic_selector.select_mode）──
# 優先序 cascade：只有一種 lane 有素材 → 直接選；兩者都有 → 輪替避免每次都選同一種；
# 兩者皆空 → 'default'（沿用現有排序，不強制切換）。這是排序優先度層，不是新推薦邏輯
# ——不動 long_tail/adjacent_artists 既有分數公式。
REC_MODE_ORDER = ("rediscover", "discover_new")
DEFAULT_REC_MODE_PATH = "records/rec_mode_state.json"
_LAST_REC_MODE_KEY = "_last_rec_mode"


class RecModeStore:
    """情境切換選擇器的輪替狀態，disk JSON 持久化（撓過重啟），fail-open。"""

    def __init__(self, path: str = DEFAULT_REC_MODE_PATH):
        self._path = path
        self._data = self._load()

    def _load(self) -> dict:
        try:
            return json.load(open(self._path, encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = f"{self._path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False)
            os.replace(tmp, self._path)
        except OSError:
            pass  # fail-open：寫不進去不影響功能（下次再判斷）

    def get_last_mode(self) -> str | None:
        return self._data.get(_LAST_REC_MODE_KEY)

    def set_last_mode(self, mode: str) -> None:
        self._data[_LAST_REC_MODE_KEY] = mode
        self._save()


def select_rec_mode(
    *,
    has_rediscover: bool,
    has_discover_new: bool,
    store: RecModeStore,
) -> str:
    """本地決定這輪自動推薦要走哪個情境模式，不用每次問 LLM。

    回傳 'rediscover' | 'discover_new' | 'default'：
      - 只有 rediscover 有素材（long_tail lane 非空）→ 'rediscover'
      - 只有 discover_new 有素材（adjacent_artists 非空）→ 'discover_new'
      - 兩者都有 → 在兩者間輪替（不重複上次選過的），避免每次都落在同一種
      - 兩者皆空 → 'default'（fallback 現有預設排序，不強制切換）
    """
    candidates = [
        m for m in REC_MODE_ORDER
        if (m == "rediscover" and has_rediscover)
        or (m == "discover_new" and has_discover_new)
    ]
    if not candidates:
        return "default"
    last = store.get_last_mode()
    mode = next((m for m in candidates if m != last), candidates[0])
    store.set_last_mode(mode)
    return mode


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
