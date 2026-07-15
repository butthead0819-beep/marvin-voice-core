"""DJ tail-crossfade scheduling helper.

Pure function — no IO, no imports of external services.
"""
from __future__ import annotations


def tail_dj_fire_delay(
    duration_s: float | None,
    elapsed_s: float,
    lead_s: float = 5.0,
    min_song_s: float = 30.0,
) -> float | None:
    """滑動窗點火：歌1 結束前 lead_s 秒開始播 DJ（疊歌1尾段、溢進歌2開頭）。

    點火錨定在「歌1 結束前 lead_s」，與 DJ 長度無關——DJ 長度決定它溢進歌2
    開頭多少（DJ ~15s、lead=5s → 5s 疊歌1尾巴 + ~10s 疊歌2開頭）。

    Returns None when the tail-crossfade cannot/should not fire:
      - duration unknown (None or 0)
      - song shorter than min_song_s
      - fire window already passed (fire_at <= elapsed_s)

    Otherwise returns max(0.0, fire_at - elapsed_s).
    """
    if not duration_s:   # None or 0
        return None
    if duration_s < min_song_s:
        return None

    fire_at = duration_s - lead_s
    if fire_at <= elapsed_s:
        return None

    return max(0.0, fire_at - elapsed_s)
