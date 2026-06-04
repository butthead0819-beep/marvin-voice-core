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
