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
from typing import Awaitable, Callable

from music_recommender import Candidate, pick_candidate, pick_candidates

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


async def resolve_car_open_query(
    song: Candidate | None,
    *,
    pool_provider: Callable[[], list[Candidate]],
    resolve_fn: Callable[[str], Awaitable[dict | None]],
    max_attempts: int = 3,
) -> str | None:
    """挑開場曲最終要拿去點播的查詢字串，過 track_quality 的非單曲品質閘。

    2026-08-13 review 補：原本第一首candidate沒過品質閘就直接放棄，開場靜音、
    駕駛沒任何提示。改成最多試 max_attempts 首——先試 song，沒過再從
    pool_provider() 補抽候選池（排除已試過的），直到抽到一首過閘的或用完次數。

    resolve_fn(query) 拋例外（逾時/服務不可用，由 caller 決定要不要包 timeout）視為
    「無法驗證，保守放行」——回傳當前這首的 query 讓 caller 照樣嘗試播放，不因為
    resolve 掛掉就連音樂都沒有（車載開場的體驗優先序：有聲音 > 沒過驗證）。
    ⚠️ 2026-08-17 車puck mk2 實機踩到：resolve_fn 乾淨回 None（真的搜不到，不是
    例外）原本被跟「掛掉」同樣處理直接放行，結果送出一個已知搜不到的 query，
    caller 二次 resolve 一樣失敗、整趟開場靜音。改成乾淨回 None＝這首確定不行，
    换下一首候選試——只有真的拋例外才保守放行。
    全部候選都沒過品質閘/都搜不到 → 回傳 None，caller 決定開場靜音。
    """
    if song is None:
        return None
    from track_quality import is_non_song_video

    candidates = [song]
    tried_titles: set[str] = set()
    for _ in range(max_attempts):
        if not candidates:
            more = pick_candidates(pool_provider() or [], k=3)
            candidates = [c for c in more if c.anchor_title not in tried_titles]
            if not candidates:
                return None
        cur = candidates.pop(0)
        tried_titles.add(cur.anchor_title)
        query = cur.direct_url or (f"{cur.anchor_artist} {cur.anchor_title}".strip() or cur.anchor_title)
        try:
            info = await resolve_fn(query)
        except Exception:
            return query   # 真的異常（逾時/服務不可用）→ 保守放行，不繼續試下一首
        if info is None:
            continue        # 乾淨回 None＝真的搜不到，換下一首候選試
        is_ns, _reason = is_non_song_video(info.get("title", ""), info.get("duration"))
        if is_ns:
            continue
        return info.get("webpage_url") or query
    return None


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
