"""使用者點歌兩道防護：30s 去重 ledger + yt-dlp 解析 TTL 快取（2026-07-04）。

① RecentRequestLedger：同 speaker + 同 videoId + 窗內 → 重複，跳過入隊。
   背景：佇列去重（check_history=False）只看佇列——第一發已 pop 去播時
   佇列是空的，第二發漏過（7/3-4 debounce 殘餘實錘）。ledger 與佇列狀態
   無關，治所有重複形式（debounce 殘餘/語音+手動混點/誤觸連點）。
   窗外（>30s）同曲重點 = 真心想再聽，放行。

② ResolveCache：videoId → yt-dlp info 的 TTL 快取。抽流 ~2s 是點歌 5 秒
   延遲中最大可壓縮塊；使用模式重複點播極多（同曲一晚 ×5 常見），
   重複點播免重抽。TTL 60min（串流 URL 壽命 ~6h，保守取 1h）；
   get 回 shallow copy——caller 會就地改 requested_by/_lane，不得污染快取。
"""
from __future__ import annotations

# 拼音 fuzzy（C'）：收「糊法飄動的重複點播」——同一首歌被 STT 糊成不同字（消防器的慢歌
# ／消防器的慢／消防氣的慢歌）。用 token_sort_ratio（不是 catalog 的 token_set_ratio，
# 後者對子集給 100 會讓「消防」假命中長歌名）。缺 dep → 靜默退回純精確比對。
try:
    from rapidfuzz import fuzz as _fuzz
    from music_fastpath import to_pinyin as _to_pinyin
    _FUZZY_OK = True
except ImportError:
    _FUZZY_OK = False

DEFAULT_WINDOW_S = 30.0
DEFAULT_TTL_S = 3600.0
_FUZZY_THRESHOLD = 85.0   # 真 repeat ≥91 / 假命中 ≤63，85 乾淨分離
_FUZZY_MIN_TOKENS = 3     # 拼音 token <3 太短易假命中 → 不 fuzzy


class RecentRequestLedger:
    def __init__(self, window_s: float = DEFAULT_WINDOW_S):
        self._window = window_s
        self._seen: dict[tuple[str, str], float] = {}

    def is_dup(self, speaker: str, video_id: str, now: float) -> bool:
        ts = self._seen.get((speaker, video_id))
        return ts is not None and (now - ts) <= self._window

    def mark(self, speaker: str, video_id: str, now: float) -> None:
        # 順手 prune 過期項（量小，每次 mark 掃一遍便宜）
        self._seen = {k: v for k, v in self._seen.items() if now - v <= self._window}
        self._seen[(speaker, video_id)] = now

    def size(self) -> int:
        return len(self._seen)


class ResolveCache:
    def __init__(self, ttl_s: float = DEFAULT_TTL_S):
        self._ttl = ttl_s
        self._cache: dict[str, tuple[dict, float]] = {}

    def get(self, video_id: str, now: float) -> dict | None:
        hit = self._cache.get(video_id)
        if hit is None:
            return None
        info, ts = hit
        if now - ts > self._ttl:
            del self._cache[video_id]
            return None
        return dict(info)   # shallow copy：防 caller 就地污染

    def put(self, video_id: str, info: dict | None, now: float) -> None:
        if not video_id or not isinstance(info, dict):
            return
        # 同樣順手 prune
        self._cache = {k: v for k, v in self._cache.items() if now - v[1] <= self._ttl}
        self._cache[video_id] = (dict(info), now)


def _normalize_query_key(query: str) -> str:
    """點歌 query 正規化成快取鍵：去頭尾/內部空白 + 轉小寫。

    STT 糊字（器/體/奇）仍會落在不同鍵——這層只治「一致重複」的點播
    （同一首歌反覆點、清楚咬字或穩定糊法），是嚴格增益、命中即省 ~6s 搜尋。
    """
    return "".join((query or "").split()).lower()


class QueryResolveCache:
    """正規化 query → {webpage_url, title} 的持久化快取。

    現有 ResolveCache 只治 videoId→info（URL 直點才受益）；文字查詢每次都跑
    ytsearch5(~6s)。這層記住「點過的歌名→videoId」，命中則改走 URL 解析跳搜尋。
    只存 webpage_url（穩定），不存串流 url（會過期）。持久化到 JSON，跨重啟受益。
    """

    def __init__(self, path: str | None = "records/query_resolve_cache.json", max_size: int = 2000):
        self._path = path
        self._max = max_size
        self._cache: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self._path:
            return
        try:
            import json
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._cache = {k: v for k, v in data.items() if isinstance(v, dict)}
        except (FileNotFoundError, ValueError, OSError):
            self._cache = {}

    def _save(self) -> None:
        if not self._path:
            return
        try:
            import json
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False)
        except OSError:
            pass

    def get(self, query: str) -> dict | None:
        hit = self._cache.get(_normalize_query_key(query))
        if hit:
            return {"webpage_url": hit["webpage_url"], "title": hit.get("title", "")}
        return self._fuzzy_get(query)   # 精確 miss → 拼音 fuzzy 收糊字漂移

    def _fuzzy_get(self, query: str) -> dict | None:
        if not _FUZZY_OK:
            return None
        qpy = _to_pinyin(query)
        if len(qpy.split()) < _FUZZY_MIN_TOKENS:
            return None
        best, best_score = None, 0.0
        for v in self._cache.values():
            cand_py = v.get("pinyin") or ""
            if not cand_py:
                continue
            s = _fuzz.token_sort_ratio(qpy, cand_py)
            if s > best_score:
                best_score, best = s, v
        if best is not None and best_score >= _FUZZY_THRESHOLD:
            return {"webpage_url": best["webpage_url"], "title": best.get("title", "")}
        return None

    def put(self, query: str, webpage_url: str, title: str) -> None:
        key = _normalize_query_key(query)
        if not key or not webpage_url:
            return
        # 淘汰最舊（dict 保序，超上限先移除最早插入的）
        while len(self._cache) >= self._max and self._cache:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = {
            "webpage_url": webpage_url,
            "title": title,
            "pinyin": _to_pinyin(query) if _FUZZY_OK else "",
        }
        self._save()

    def delete(self, query: str) -> None:
        """快取的影片下架/失效 → 清掉，下次重新搜尋（防永久走失效 URL）。"""
        if self._cache.pop(_normalize_query_key(query), None) is not None:
            self._save()
