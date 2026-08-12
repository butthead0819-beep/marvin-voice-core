"""LRC 同步歌詞 → 前奏結束點，給長前奏跳過起播用。

長前奏（純器樂/口白/環境音）讓歌曲開播要等很久才聽到主旋律；抓 LRC 第一句
「實質歌詞」的時間戳，起播點往前奏尾巴挪，保留 _LEAD_IN_S 前導伴奏再切入主歌。
跟 youtube_heatmap.pick_highlight_start 同樣 fail-open：抓不到合理起點就回 None
（=從頭播，維持舊行為）。
"""
from __future__ import annotations

from intent_agents.lyrics_seek import parse_lrc

_INTRO_THRESHOLD_S = 10.0
_LEAD_IN_S = 1.5
_MIN_REMAINING_S = 30.0


def pick_intro_skip_start(lrc: str | None, duration: float | None) -> float | None:
    """挑 LRC 第一句實質歌詞的時間戳往前推 _LEAD_IN_S 秒當起播點。

    放棄條件：沒 LRC / 沒 duration / 第一句歌詞在 _INTRO_THRESHOLD_S 秒內出現
    （前奏不長，跳過去意義不大）/ 跳過去的起點離結尾剩不到 _MIN_REMAINING_S 秒 /
    起點超過總長一半（LRC 抓錯第一句實質歌詞時，避免整首歌被跳掉大半）。
    """
    if not lrc or not duration:
        return None
    first_lyric_ts = next(
        (ts for ts, text in parse_lrc(lrc) if text.strip()), None
    )
    if first_lyric_ts is None or first_lyric_ts < _INTRO_THRESHOLD_S:
        return None
    start = first_lyric_ts - _LEAD_IN_S
    if duration - start < _MIN_REMAINING_S:
        return None
    if start > duration / 2:
        return None
    return start
