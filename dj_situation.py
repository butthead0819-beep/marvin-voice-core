"""DJ atmosphere 模式的「現況」素材。

三種現況：平日上班時間、Marvin 這次開台多久（bot 啟動時間 `bot.last_restart_time`）、
在場者這次在頻道待多久（`data/voice_presence.jsonl`，見 presence_logger.py）。

死角：presence log 只在 Marvin 已在頻道時記錄 join/leave，所以「join 早於 bot 啟動
時間」或找不到 join 一律當不知道、不出素材。
"""
from __future__ import annotations

import datetime
import json
import os

WORK_START_HOUR = 8
WORK_END_HOUR = 17          # 不含：17:00 起算下班
MIN_DURATION_S = 30 * 60    # 待不到 30 分鐘不值得講
PRESENCE_LOG_PATH = "data/voice_presence.jsonl"
_TAIL_BYTES = 512 * 1024    # 只讀檔尾，log 會一直長


def is_working_hours(dt: datetime.datetime) -> bool:
    return dt.weekday() < 5 and WORK_START_HOUR <= dt.hour < WORK_END_HOUR


def format_duration_zh(seconds: float | None) -> str | None:
    if seconds is None or seconds < MIN_DURATION_S:
        return None
    if seconds < 3600:
        return f"{int(seconds // 600) * 10} 分鐘"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小時"
    return f"{int(seconds // 86400)} 天"


def load_join_times(path: str | None = None, *, since_ts: float) -> dict[str, float]:
    if path is None:
        path = PRESENCE_LOG_PATH
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            truncated = size > _TAIL_BYTES
            if truncated:
                f.seek(size - _TAIL_BYTES)
            data = f.read()
    except OSError:
        return {}

    lines = data.decode("utf-8", errors="ignore").splitlines()
    if truncated:
        lines = lines[1:]  # 第一行可能被切半

    latest: dict[str, float] = {}
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        name = record.get("user_name")
        if not isinstance(name, str) or not name:
            continue
        event = record.get("event")
        if event == "join":
            try:
                latest[name] = float(record["ts"])
            except (KeyError, TypeError, ValueError):
                continue
        elif event == "leave":
            latest.pop(name, None)
    return {name: ts for name, ts in latest.items() if ts >= since_ts}


def situation_facts(
    now: float,
    *,
    on_air_since: float | None,
    join_times: dict[str, float],
    present_members: set[str] | None,
) -> list[str]:
    facts: list[str] = []
    if is_working_hours(datetime.datetime.fromtimestamp(now)):
        facts.append("現在是平日上班時間，大家可能正邊上班邊聽")
    if on_air_since is not None:
        d = format_duration_zh(now - on_air_since)
        if d is not None:
            facts.append(f"Marvin 電台這次已經連續開台 {d}")
    for name in sorted(present_members or ()):
        ts = join_times.get(name)
        if ts is None:
            continue
        d = format_duration_zh(now - ts)
        if d is not None:
            facts.append(f"{name} 這次已經在頻道待了 {d}")
    return facts
