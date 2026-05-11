"""
離場習慣統計模組。

記錄每位玩家的真實離場時間，用於預測「這個人現在說 bye 是真的要走嗎」。
資料存 departure_stats.json，每人最多保留 200 筆。
"""

import asyncio
import json
import os
import time
import logging
from datetime import datetime
from collections import Counter

logger = logging.getLogger(__name__)

_PATH = os.path.join(os.path.dirname(__file__), "departure_stats.json")
_MAX_RECORDS = 200


class DepartureStats:
    def __init__(self):
        self._data: dict = self._load()

    # ------------------------------------------------------------------ #
    # I/O                                                                  #
    # ------------------------------------------------------------------ #

    def _load(self) -> dict:
        try:
            with open(_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self):
        tmp = _PATH + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, _PATH)
        except Exception as e:
            logger.warning(f"[DepartureStats] 寫入失敗: {e}")

    # ------------------------------------------------------------------ #
    # 寫入                                                                 #
    # ------------------------------------------------------------------ #

    async def record_departure(self, speaker: str, verbal_bye: bool):
        """記錄一次真實離場事件。

        verbal_bye: 離場前是否說了 bye（Farewell Detector 有偵測到）
        """
        now_dt = datetime.now()
        entry = {
            "ts": time.time(),
            "weekday": now_dt.weekday(),   # 0=Mon … 6=Sun
            "hour": now_dt.hour,
            "minute": now_dt.minute,
            "verbal_bye": verbal_bye,
        }
        user = self._data.setdefault(speaker, {
            "departures": [],
            "false_alarms": 0,
            "last_updated": 0,
        })
        user["departures"].append(entry)
        if len(user["departures"]) > _MAX_RECORDS:
            user["departures"] = user["departures"][-_MAX_RECORDS:]
        user["last_updated"] = time.time()
        await asyncio.to_thread(self._save)
        logger.info(
            f"[DepartureStats] 記錄離場 | {speaker} | "
            f"weekday={now_dt.strftime('%a')} hour={now_dt.hour:02d}:{now_dt.minute:02d} "
            f"verbal_bye={verbal_bye}"
        )

    async def record_false_alarm(self, speaker: str):
        """記錄一次誤判：Farewell Detector 以為要走，但 25 秒後還在頻道。"""
        user = self._data.setdefault(speaker, {
            "departures": [],
            "false_alarms": 0,
            "last_updated": 0,
        })
        user["false_alarms"] = user.get("false_alarms", 0) + 1
        user["last_updated"] = time.time()
        await asyncio.to_thread(self._save)
        logger.info(f"[DepartureStats] 記錄誤判 | {speaker} | 累計誤判={user['false_alarms']}")

    # ------------------------------------------------------------------ #
    # 查詢 / 預測                                                          #
    # ------------------------------------------------------------------ #

    def predict_leaving_soon(self, speaker: str, window_minutes: int = 30) -> float:
        """估計 speaker 在未來 window_minutes 分鐘內離場的歷史機率 (0.0 ~ 1.0)。

        優先用同星期幾的資料；不足 3 筆則 fallback 到全部資料。
        不足 3 筆全部資料時回傳 0.0（無法估計）。
        """
        now_dt = datetime.now()
        now_min = now_dt.hour * 60 + now_dt.minute
        end_min = now_min + window_minutes

        all_records = self._data.get(speaker, {}).get("departures", [])
        if not all_records:
            return 0.0

        same_day = [d for d in all_records if d.get("weekday") == now_dt.weekday()]
        pool = same_day if len(same_day) >= 3 else all_records
        if len(pool) < 3:
            return 0.0

        in_window = sum(
            1 for d in pool
            if now_min <= (d["hour"] * 60 + d["minute"]) < end_min
        )
        return round(in_window / len(pool), 2)

    def typical_departure_summary(self, speaker: str) -> str:
        """回傳人類可讀的離場習慣摘要，用於 stt_logger。

        例："最常在 22:00-23:00 離場（佔 60%），誤判率 15%"
        """
        user = self._data.get(speaker)
        if not user:
            return "（無歷史資料）"

        records = user.get("departures", [])
        if len(records) < 3:
            return f"（資料不足，共 {len(records)} 筆）"

        hour_counter = Counter(d["hour"] for d in records)
        top_hour, top_count = hour_counter.most_common(1)[0]
        pct = round(top_count / len(records) * 100)

        false_alarms = user.get("false_alarms", 0)
        verbal_byes = sum(1 for d in records if d.get("verbal_bye"))
        fa_rate = round(false_alarms / max(verbal_byes + false_alarms, 1) * 100)

        return (
            f"最常在 {top_hour:02d}:xx 離場（{pct}%，共 {len(records)} 筆）"
            f" | 誤判率 {fa_rate}%（誤判 {false_alarms} 次）"
        )

    def departure_count(self, speaker: str) -> int:
        return len(self._data.get(speaker, {}).get("departures", []))
