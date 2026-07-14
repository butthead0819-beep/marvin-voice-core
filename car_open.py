"""
car_open.py — 車載「讀空氣開場」邏輯（ESP32 puck）。

先落地時段解析：上車冷啟沒有對話 transcript，只有「時段」當 context 信號。
（開場選曲＝復用既有選曲層 + taste_fingerprint、絕不打即時付費 LLM，為下一刀。）

純函式，datetime 當參數傳（零 now() 依賴，好測）。
"""
from __future__ import annotations

import datetime as _dt

# 5 個離散 bucket（design doc / eng review）；順序不重要，成員固定。
TIME_BUCKETS = ("morning", "noon", "afternoon", "evening", "late_night")


def resolve_time_bucket(when: _dt.datetime) -> str:
    """把 datetime 落到 5 個時段 bucket 之一。

    morning 05–11 / noon 11–14 / afternoon 14–18 / evening 18–23 /
    late_night 23–05（跨午夜 wrap）。邊界＝含下界、不含上界。
    """
    h = when.hour
    if 5 <= h < 11:
        return "morning"
    if 11 <= h < 14:
        return "noon"
    if 14 <= h < 18:
        return "afternoon"
    if 18 <= h < 23:
        return "evening"
    return "late_night"   # h >= 23 或 h < 5
