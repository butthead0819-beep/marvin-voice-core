"""T2 Discovery — YouTube Music radio 候選來源（PoC，2026-06-04）。

autopilot 候選池三層的 T2：從在場者 liked 的歌 seed → ytmusicapi radio 取相關「新歌」，
擴充有限團體歌庫。官方 YouTube Data API 的 relatedToVideoId 2023 已移除，故走 ytmusicapi
的非官方 InnerTube（radio 公開、無需 auth）。

實測可用呼叫（2026-06-04 live 驗證）：
    get_watch_playlist(videoId=X, playlistId="RDAMVM"+X)
  → tracks[0] 是 seed 本身，其後為同曲風相關新歌。
（注：只給 videoId+radio=True 會撞 ytmusicapi 的 KeyError 'endpoint'，故用 RDAMVM playlistId。）

parse/filter 為純函式（可單測無網路）；ytmusic_radio 的 client 可注入。本檔尚未接進
_auto_recommend——先驗證 radio 真吐相關新歌、過得了安全閘，再接成 T1→T2→T3 鏈。
"""
from __future__ import annotations

from typing import Any, Callable, Sequence

from music_recommender import normalize_title


def parse_length(s: str | None) -> int:
    """"3:45" / "1:02:03" → 秒；解析不出回 0。"""
    if not s:
        return 0
    try:
        nums = [int(p) for p in s.split(":")]
    except (ValueError, AttributeError):
        return 0
    secs = 0
    for n in nums:
        secs = secs * 60 + n
    return secs


def parse_radio_tracks(watch_playlist: dict, *, seed_video_id: str = "") -> list[dict]:
    """get_watch_playlist 回應的 tracks → 候選 dict（純函式）。

    丟掉 seed 本身與無 videoId/title 者。回 [{title, artist, video_id, url, duration_s}]。
    """
    out: list[dict] = []
    for t in (watch_playlist or {}).get("tracks") or []:
        vid = t.get("videoId")
        title = (t.get("title") or "").strip()
        if not vid or not title:
            continue
        if seed_video_id and vid == seed_video_id:
            continue
        artist = ", ".join(
            a.get("name", "") for a in (t.get("artists") or []) if a.get("name")
        )
        out.append({
            "title": title,
            "artist": artist,
            "video_id": vid,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "duration_s": parse_length(t.get("length")),
        })
    return out


def _default_client_factory() -> Any:
    from ytmusicapi import YTMusic
    return YTMusic()


def ytmusic_radio(
    seed_video_id: str,
    *,
    exclude_titles: Sequence[str] = (),
    limit: int = 20,
    client: Any = None,
    client_factory: Callable[[], Any] = _default_client_factory,
) -> list[dict]:
    """T2 discovery：seed → radio 相關新歌，過 exclude_titles（skipped/已播）後回 ≤limit 首。

    exclude_titles 用 normalize_title 比對（與 music_recommender 一致，去變體後綴）。
    client 可注入（測試用）；未給則 lazy 建 YTMusic()。任何失敗回 []（graceful → 上層退 T3）。
    """
    if not seed_video_id:
        return []
    try:
        yt = client or client_factory()
        wp = yt.get_watch_playlist(
            videoId=seed_video_id, playlistId="RDAMVM" + seed_video_id
        )
    except Exception:
        return []
    cands = parse_radio_tracks(wp, seed_video_id=seed_video_id)
    excl = {normalize_title(t) for t in exclude_titles}
    filtered = [c for c in cands if normalize_title(c["title"]) not in excl]
    return filtered[:limit]


def parse_search_songs(results: list) -> list[dict]:
    """ytmusicapi search(filter="songs") 結果 → 候選 dict（純函式）。丟無 videoId/title。

    回 [{title, artist, video_id, url, duration_s}]，與 parse_radio_tracks 同形狀
    （好共用 blend_radio_results / 下游 Candidate 轉換）。
    """
    out: list[dict] = []
    for t in results or []:
        vid = t.get("videoId")
        title = (t.get("title") or "").strip()
        if not vid or not title:
            continue
        artist = ", ".join(
            a.get("name", "") for a in (t.get("artists") or []) if a.get("name")
        )
        dur = t.get("duration_seconds") or parse_length(t.get("duration"))
        out.append({
            "title": title,
            "artist": artist,
            "video_id": vid,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "duration_s": dur,
        })
    return out


def ytmusic_search_songs(
    query: str,
    *,
    exclude_titles: Sequence[str] = (),
    limit: int = 20,
    client: Any = None,
    client_factory: Callable[[], Any] = _default_client_factory,
) -> list[dict]:
    """T4 fresh discovery：搜尋 query 的歌 → 過 exclude_titles（skipped/已播）後回 ≤limit 首。

    給核心藝人名當 query → 拉他 catalog 裡「還沒播過」的歌＝又新又對味（radio 種子固定會
    收斂到同批相關歌，search 直接搜藝人整個曲庫更廣）。client 可注入；任何失敗回 []（graceful）。
    """
    if not query:
        return []
    try:
        yt = client or client_factory()
        results = yt.search(query, filter="songs", limit=limit)
    except Exception:
        return []
    cands = parse_search_songs(results)
    excl = {normalize_title(t) for t in exclude_titles}
    return [c for c in cands if normalize_title(c["title"]) not in excl][:limit]


def blend_radio_results(results_per_seed, exclude_titles=None, limit=None):
    """多 seed 的 radio 結果交錯混合（round-robin）+ 跨 seed 去重 + 排除 + 截斷。

    round-robin 讓每個 seed 的口味都進前段，而非單一 seed 灌滿（多 seed 混合 radio 的核心）。
    去重以 url 為主、title 為輔；exclude_titles 用 normalize_title 比對（與單 seed 一致）。
    純函式、無網路、可單測。任何 seed 結果空/缺 url 安全跳過。
    """
    from itertools import zip_longest
    excl = {normalize_title(t) for t in (exclude_titles or [])}
    seen_url: set = set()
    seen_title: set = set()
    out: list[dict] = []
    for group in zip_longest(*results_per_seed):
        for c in group:
            if not c:
                continue
            url = c.get("url")
            title = (c.get("title") or "").strip()
            if not url or url in seen_url:
                continue
            if title in seen_title or normalize_title(title) in excl:
                continue
            seen_url.add(url)
            seen_title.add(title)
            out.append(c)
            if limit and len(out) >= limit:
                return out
    return out
