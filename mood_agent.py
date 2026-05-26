"""MoodAgent — Week 3 of social-catalyst plan.

讀「文字 mood + 群體溫度 + 時段」三軸訊號，寫進 RoomMoodState，提供
action_tier 給其他 agent 參考。**自己不發話**（不繼承 SpeakAgent）。

設計來源：docs/social_catalyst_plan.md。

合約：
  - mood_sensor / temperature_monitor 兩個依賴都是 optional：None 時退化預設
    值，never raise（observe 失敗永遠 swallow）
  - 不寫 bot 自己的 mood：observe 只動 RoomMoodState.group_*，不動 individual_mood
  - 不主動建議發話：consumer (MusicAgent / BridgeAgent / DuckingAgent) 自己讀
    get_action_tier() 決定動作
  - action_tier 分四級：none / light / mid / heavy
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Callable, Literal

from room_mood_state import RoomMoodStateStore, DEFAULT_MOOD

logger = logging.getLogger(__name__)

# action tier 閾值（plan 規格）
_HEAVY_TEMP_MAX = 0.3
_HEAVY_SILENCE_MIN_S = 60.0
_MID_TEMP_MAX = 0.5

# 4-tier mood labels（mirror mood_sensor.MOOD_LABELS）
_NEGATIVE_MOODS = frozenset({"低落"})
_MID_MOODS = frozenset({"低落", "分歧"})

ActionTier = Literal["none", "light", "mid", "heavy"]


class MoodAgent:
    name: str = "MoodAgent"

    def __init__(
        self,
        mood_store: RoomMoodStateStore,
        *,
        mood_sensor=None,           # has async current_vibe(guild_id) -> obj with .mood
        temperature_monitor=None,   # has .temperature (0.0-1.0)
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = mood_store
        self._sensor = mood_sensor
        self._temp = temperature_monitor
        self._clock = clock

    # ── dependency wiring (main_discord 後置注入用) ───────────────────────────

    def wire_dependencies(self, *, mood_sensor=None, temperature_monitor=None) -> None:
        """讓 main_discord.py 在 mood_sensor 建好後注入。給 None 不覆蓋。"""
        if mood_sensor is not None:
            self._sensor = mood_sensor
        if temperature_monitor is not None:
            self._temp = temperature_monitor

    # ── observe (async；mood_sensor 走 LLM) ──────────────────────────────────

    async def observe(self, channel_id: int, guild_id: int) -> dict:
        """跑一次三軸合成，寫 mood_store，回 snapshot dict 供 caller 看。

        Never raises. 任一依賴失敗 → 退化預設值，繼續寫 store。
        """
        # 文字 mood（從 mood_sensor LLM 分類）
        mood = DEFAULT_MOOD
        if self._sensor is not None:
            try:
                vibe = await self._sensor.current_vibe(guild_id=guild_id)
                mood = getattr(vibe, "mood", DEFAULT_MOOD) or DEFAULT_MOOD
            except Exception as e:
                logger.warning("[MoodAgent] mood_sensor 失敗，退預設 %s: %s", DEFAULT_MOOD, e)

        # 群體溫度（temperature_monitor 是 source of truth）
        temperature = 0.0
        if self._temp is not None:
            try:
                temperature = float(getattr(self._temp, "temperature", 0.0))
            except (TypeError, ValueError) as e:
                logger.warning("[MoodAgent] temperature_monitor 讀取失敗: %s", e)

        # 寫進 store（只動 group_*）
        self._store.set_group(channel_id, mood=mood, temperature=temperature)

        return {
            "mood": mood,
            "temperature": temperature,
            "time_bucket": self.time_bucket(),
        }

    # ── time bucket（pure，無 IO）────────────────────────────────────────────

    def time_bucket(self) -> str:
        """morning(6-12) / afternoon(12-18) / evening(18-23) / late_night(23-6)

        以 UTC hour 算。本地化在 consumer 端做（不同 guild 不同時區）。
        """
        hour = datetime.fromtimestamp(self._clock(), tz=timezone.utc).hour
        if 6 <= hour < 12:
            return "morning"
        if 12 <= hour < 18:
            return "afternoon"
        if 18 <= hour < 23:
            return "evening"
        return "late_night"

    # ── action tier（pure，讀 store）──────────────────────────────────────────

    def get_action_tier(self, channel_id: int, *, silence_seconds: float = 0.0) -> ActionTier:
        """讀 mood_store + silence_seconds → 行動級。

        - heavy: 低落 + 低溫 + 群體靜默 ≥ 60s → bot 該退
        - mid:   低落 / 分歧 + 中低溫 → 該丟 bridge seed
        - light: 單純 mood 偏負 → MusicAgent 調整下首
        - none:  其他
        """
        state = self._store.get(channel_id)
        mood = state.group_mood
        temp = state.group_temperature

        if mood in _NEGATIVE_MOODS and temp <= _HEAVY_TEMP_MAX and silence_seconds >= _HEAVY_SILENCE_MIN_S:
            return "heavy"
        if mood in _MID_MOODS and temp <= _MID_TEMP_MAX:
            return "mid"
        if mood in _NEGATIVE_MOODS:
            return "light"
        return "none"
