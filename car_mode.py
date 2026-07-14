"""
car_mode.py — 車載模式 wiring（ESP32 puck 上車開場 / 下車停播）。

把 CarPresence 的 on_arrive/on_depart 接到「讀時段 → 建開場 → 播放/停止」：
- on_arrive: resolve_time_bucket(now) → build_car_open(復用選曲層、絕不付費 LLM)
             → 呼叫注入的 play_open(car_open)
- on_depart / TTL 逾時（熄火）: 呼叫注入的 stop_playback()

play_open / stop_playback / pool_provider 都注入（真實綁定在 main_satellite 組裝處），
∴ 本模組純邏輯、無 Discord / 播放副作用、好測。
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import time as _time
from typing import Awaitable, Callable

from car_open import build_car_open, resolve_time_bucket
from car_presence import CarPresence
from music_recommender import Candidate

# 每 bucket 預生成開場白（免費、離線批次可再擴充；絕不即時付費 LLM）。
DEFAULT_OPEN_LINES: dict[str, list[str]] = {
    "morning":    ["早安，來點音樂醒醒腦。", "早上好，我挑首歌陪你出門。"],
    "noon":       ["午安，來首歌配午餐。", "中午了，放點輕鬆的。"],
    "afternoon":  ["下午好，來首歌提神。", "午後時光，放首順的。"],
    "evening":    ["晚上好，放首歌配晚風。", "傍晚了，來點對味的。"],
    "late_night": ["夜深了，放首安靜的。", "深夜檔，來首 city pop。"],
}


def build_car_presence(
    *,
    play_open: Callable[[object], Awaitable[None]],
    stop_playback: Callable[[], Awaitable[None]],
    pool_provider: Callable[[], list[Candidate]],
    open_lines: dict[str, list[str]] | None = None,
    now_fn: Callable[[], _dt.datetime] | None = None,
    ttl_s: float = 90.0,
    time_fn: Callable[[], float] | None = None,
) -> CarPresence:
    """組出接好開場/停播的 CarPresence。

    now_fn＝取現在時間（決定時段 bucket，牆鐘）；time_fn＝TTL 用的單調時鐘。
    兩者分開：bucket 要時刻、TTL 要經過時間。
    """
    lines = open_lines or DEFAULT_OPEN_LINES
    _now = now_fn or _dt.datetime.now

    async def on_arrive() -> None:
        car_open = build_car_open(
            resolve_time_bucket(_now()),
            pool_provider=pool_provider,
            open_lines=lines,
        )
        await play_open(car_open)

    async def on_depart() -> None:
        await stop_playback()

    return CarPresence(
        on_arrive=on_arrive,
        on_depart=on_depart,
        ttl_s=ttl_s,
        time_fn=time_fn or _time.monotonic,
    )


async def car_ttl_tick(car_presence: CarPresence) -> bool:
    """單次 TTL 檢查（背景迴圈的一拍）；回 True＝這拍判定逾時並停播。"""
    return await car_presence.check_ttl()


async def run_car_ttl_loop(
    car_presence: CarPresence,
    *,
    interval_s: float = 10.0,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    """背景迴圈：週期性 check_ttl，讓熄火(heartbeat 停)在 TTL 後被判 absent 停播。"""
    while should_stop is None or not should_stop():
        try:
            await car_presence.check_ttl()
        except Exception:  # noqa: BLE001 — 一拍失敗不弄垮迴圈
            pass
        await sleep_fn(interval_s)
