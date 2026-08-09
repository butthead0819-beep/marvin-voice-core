"""
離場習慣統計模組。

記錄每位玩家的真實離場時間，供 on_voice_state_update 的離場記錄使用。
資料存 departure_stats.json，每人最多保留 200 筆。

8/9：原本用於「聽到 bye 就預測會不會真的走」的側通道偵測（predict_leaving_soon /
typical_departure_summary / record_false_alarm）已移除，改用 FarewellAgent
（intent_agents/farewell_agent.py）處理喚醒後直接道別；這裡只剩單純的離場事件記錄。
"""

import asyncio
import json
import os
import time
import logging
from datetime import datetime

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

        verbal_bye: 離場前是否對 Marvin 說了 bye（見 FarewellAgent）
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

    # ------------------------------------------------------------------ #
    # 查詢                                                                 #
    # ------------------------------------------------------------------ #

    def departure_count(self, speaker: str) -> int:
        return len(self._data.get(speaker, {}).get("departures", []))
