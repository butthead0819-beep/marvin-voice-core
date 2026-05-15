"""
discord_temperature_monitor.py — Discord 聊天室溫度監控器。

溫度公式：
  text_temp  = 5 分鐘內文字事件數 / 5（events/分鐘）
  voice_temp = 5 分鐘內語音事件數 / 5（events/分鐘）
  combined   = 0.4 * text_temp + 0.6 * voice_temp

等級：COLD < 0.5 / WARM 0.5-2.0 / HOT > 2.0

LowTempTrigger：
  - 連續 3 分鐘 COLD 才觸發
  - 觸發後 10 分鐘 cooldown
  - 每 session 最多 3 次觸發
  - reset_session() 重置計數器

ConfirmationContext：
  - 觸發時 TTS「最近有點安靜，要我出個話題嗎？」
  - 開 wake_detector.temporary_open_window(30, reason="topic_confirm")
  - on_stt_result 收到肯定回覆 → trigger topic_generator.generate_topics()
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from utils.affirmative import is_affirmative

logger = logging.getLogger(__name__)

# ── 常數 ──────────────────────────────────────────────────────────────────────

_WINDOW_SECONDS    = 5 * 60      # 5 分鐘滾動視窗
_WINDOW_MINUTES    = 5.0         # 除數

_COLD_THRESHOLD    = 0.5
_HOT_THRESHOLD     = 2.0

_COLD_STREAK_NEED  = 3           # 連續幾分鐘 COLD 才觸發
_COOLDOWN_SECONDS  = 10 * 60     # 10 分鐘 cooldown
_SESSION_CAP       = 3           # 每 session 最多觸發次數

_CONFIRM_WINDOW_S  = 30          # 確認視窗秒數
_TTS_PROMPT        = "最近有點安靜，要我出個話題嗎？"


# ── DiscordTemperatureMonitor ─────────────────────────────────────────────────

class DiscordTemperatureMonitor:
    """Discord 聊天室溫度監控器。"""

    def __init__(
        self,
        wake_detector,
        tts_fn,
        topic_generator,
        companion_bridge=None,
    ):
        self._wake_detector   = wake_detector
        self._tts_fn          = tts_fn
        self._topic_generator = topic_generator
        self.companion_bridge = companion_bridge

        # 事件時間戳
        self._msg_times: deque[float]   = deque()
        self._voice_times: deque[float] = deque()

        # LowTempTrigger 狀態
        self._cold_streak:    int   = 0
        self._last_trigger:   float = 0.0   # epoch；0 → 從未觸發
        self._session_count:  int   = 0

        # ConfirmationContext 狀態
        self._pending_confirm: bool = False

    # ── 公開 API ──────────────────────────────────────────────────────────────

    def record_message_event(self, channel_id: str) -> None:
        """記錄一個文字訊息事件。"""
        now = time.time()
        self._msg_times.append(now)
        self._prune_old()

    def record_voice_event(self, user_id: str) -> None:
        """記錄一個語音說話事件。"""
        now = time.time()
        self._voice_times.append(now)
        self._prune_old()

    def on_stt_result(self, text: str, user_id: str) -> None:
        """STT 轉錄結果回呼（同步）。若 pending confirm 且肯定 → 觸發 topic generator。"""
        if not self._pending_confirm:
            return
        if is_affirmative(text):
            self._pending_confirm = False
            asyncio.ensure_future(self._run_topic_generator_and_emit())
            logger.info("[TempMonitor] 肯定回覆 → 觸發 topic generator")
        else:
            self._pending_confirm = False
            logger.info("[TempMonitor] 否定/無關回覆 → 取消確認")

    async def _run_topic_generator_and_emit(self) -> None:
        """執行 topic generator 並在成功後廣播 topic_generated 事件。"""
        try:
            topics = await self._topic_generator.generate_topics()
            if self.companion_bridge and topics:
                asyncio.ensure_future(
                    self.companion_bridge.emit_topic_generated(topics, "auto")
                )
        except Exception:
            logger.exception("[TempMonitor] topic generator 執行失敗")

    def reset_session(self) -> None:
        """Jack 離開語音頻道時呼叫，重置 session 計數器。"""
        self._session_count  = 0
        self._cold_streak    = 0
        self._pending_confirm = False
        logger.info("[TempMonitor] Session 重置")

    async def check_and_trigger(self) -> None:
        """每分鐘由外部 asyncio task 呼叫一次，評估是否需要觸發。"""
        self._prune_old()
        level = self.level

        try:
            if level != "cold":
                # 溫度不 cold → 清除連續計數
                self._cold_streak = 0
                return

            self._cold_streak += 1
            logger.debug(f"[TempMonitor] cold_streak={self._cold_streak}, temp={self.temperature:.3f}")

            if self._cold_streak < _COLD_STREAK_NEED:
                return

            # 達到連續 3 分鐘 COLD —— 先檢查 cooldown 和 session cap
            now = time.time()
            if now - self._last_trigger < _COOLDOWN_SECONDS:
                logger.debug("[TempMonitor] 仍在 cooldown 中，跳過")
                return

            if self._session_count >= _SESSION_CAP:
                logger.debug("[TempMonitor] 已達 session cap，跳過")
                return

            # 執行觸發
            self._cold_streak   = 0
            self._last_trigger  = now
            self._session_count += 1
            self._pending_confirm = True

            logger.info(f"[TempMonitor] LowTempTrigger #{self._session_count} — TTS + open window")

            await self._tts_fn(_TTS_PROMPT)
            self._wake_detector.temporary_open_window(_CONFIRM_WINDOW_S, reason="topic_confirm")

        finally:
            # 廣播溫度更新（每次 check 都廣播，不論是否觸發）
            if self.companion_bridge:
                asyncio.ensure_future(
                    self.companion_bridge.emit_temperature_update(self.level, self.temperature)
                )

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def temperature(self) -> float:
        """綜合溫度 = 0.4 * text_temp + 0.6 * voice_temp"""
        n_msg   = len(self._msg_times)
        n_voice = len(self._voice_times)
        text_temp  = n_msg   / _WINDOW_MINUTES
        voice_temp = n_voice / _WINDOW_MINUTES
        return 0.4 * text_temp + 0.6 * voice_temp

    @property
    def level(self) -> str:
        """'cold' | 'warm' | 'hot'"""
        t = self.temperature
        if t < _COLD_THRESHOLD:
            return "cold"
        if t <= _HOT_THRESHOLD:
            return "warm"
        return "hot"

    # ── 私有 helpers ──────────────────────────────────────────────────────────

    def _prune_old(self) -> None:
        """清除 5 分鐘視窗外的舊事件時間戳。"""
        cutoff = time.time() - _WINDOW_SECONDS
        while self._msg_times and self._msg_times[0] < cutoff:
            self._msg_times.popleft()
        while self._voice_times and self._voice_times[0] < cutoff:
            self._voice_times.popleft()
