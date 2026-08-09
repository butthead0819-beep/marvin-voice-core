"""YouTube「最多人重播」熱力圖（yt-dlp `heatmap` 欄位）→ 精華片段起點。

heatmap 格式：[{'start_time': float, 'end_time': float, 'value': float(0-1)}, ...]，
是 yt-dlp 反解 YouTube 內部 API 拿到的，不是官方文件保證的欄位——不是每支影片都有，
格式也可能被 YouTube 改掉，全程 fail-open（拿不到就回 None＝退回原行為從頭播）。
"""
from __future__ import annotations

import os


def enabled() -> bool:
    return os.getenv("MARVIN_HIGHLIGHT_START", "1") == "1"


def pick_highlight_start(
    heatmap: list[dict] | None,
    duration: float | None,
    *,
    min_duration: float = 90.0,
    min_remaining: float = 45.0,
) -> float | None:
    """挑熱度最高的片段起點當播放起點；沒有合理起點就回 None（=從頭播）。

    放棄條件：關 flag / 沒 heatmap / 全曲短於 min_duration（跳過去意義不大）/
    挑到的起點離結尾剩不到 min_remaining 秒（幾乎播不到東西就結束）。
    """
    if not enabled() or not heatmap or not duration or duration < min_duration:
        return None
    best = max(heatmap, key=lambda seg: seg.get("value", 0) or 0)
    start = best.get("start_time")
    if start is None or start < 0:
        return None
    if duration - start < min_remaining:
        return None
    return float(start)
