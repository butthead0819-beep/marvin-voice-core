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

DEFAULT_WINDOW_S = 30.0
DEFAULT_TTL_S = 3600.0


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
