"""
car_open.py — 車載「讀空氣開場」邏輯（ESP32 puck）。

先落地時段解析：上車冷啟沒有對話 transcript，只有「時段」當 context 信號。
（開場選曲＝復用既有選曲層 + taste_fingerprint、絕不打即時付費 LLM，為下一刀。）

純函式，datetime 當參數傳（零 now() 依賴，好測）。
"""
from __future__ import annotations

import datetime as _dt
import random as _random
from dataclasses import dataclass
from typing import Callable

from music_recommender import Candidate, pick_candidate

# 5 個離散 bucket（design doc / eng review）；順序不重要，成員固定。
TIME_BUCKETS = ("morning", "noon", "afternoon", "evening", "late_night")

# open_lines 缺該 bucket / 空 → 用這句保底（絕不因缺快取就沉默）。
_FALLBACK_OPEN_LINE = "上車了，我來挑首歌。"


@dataclass
class CarOpen:
    line: str                    # 開場白（預生成快取，免費）
    song: Candidate | None       # 開場曲（復用 pick_candidate；沒候選→None，caller 降級）


def build_car_open(
    bucket: str,
    *,
    pool_provider: Callable[[], list[Candidate]],
    open_lines: dict[str, list[str]] | None,
    rng: _random.Random | None = None,
) -> CarOpen:
    """時段快取開場：挑一句預生成開場白 + 復用既有 pick_candidate 抽開場曲。

    pool_provider() → 車載候選池（MVP＝機主，由 caller 用既有 build_*_pool 供）。
    open_lines＝每 bucket 預生成的開場白（夜間離線批次產、免費）。
    ⚠️ 純確定性 Python + 復用純函式 selector，**絕不打付費 LLM**（付費鐵則）。
    """
    r = rng or _random
    lines = (open_lines or {}).get(bucket) or []
    line = r.choice(lines) if lines else _FALLBACK_OPEN_LINE
    song = pick_candidate(pool_provider() or [], rng=rng)   # pool 空 → None
    return CarOpen(line=line, song=song)


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
