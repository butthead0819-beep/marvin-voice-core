"""
YtBlockAlertMixin — 連續「0 秒秒跳」(疑 YouTube 403) 時 DM owner 更新 cookies。

2026-10-30：YouTube 對這台 Mac 的 IP 節流，ffmpeg 開串流網址直接 403，歌被秒跳，
重試也一樣 403 → 連續重武裝觸發 watchdog，但使用者完全不知道原因。這裡把「連續
N 首秒跳」轉成一則 DM，講清楚該做什麼（更新 cookies）。純觀測：失敗吞掉、不拖慢播放。

放獨立 mixin 而非 MusicCog 本體：music_cog 有 size budget 棘輪（不准長功能）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

logger = logging.getLogger(__name__)

YT_BLOCK_WINDOW_S = 120.0      # 統計視窗
YT_BLOCK_MIN_FAILS = 3         # 視窗內連續秒跳達此數 → 判定 YouTube 擋線
YT_BLOCK_ALERT_DEBOUNCE_S = 3600.0   # 同一波事故 1 小時內只 DM 一次

ALERT_TEXT = (
    "🚨 [YouTube 擋線] {n} 首歌在 {w:.0f}s 內 0 秒就中斷（ffmpeg 開串流網址疑似 403）。\n"
    "最近一首：{title}\n"
    "多半是 YouTube cookies 失效或 IP 被節流。請處理：\n"
    "1. 確認 Chrome 的 YouTube 仍是登入狀態（bot 預設讀 Chrome cookies）\n"
    "2. 重新匯出 ~/.config/marvin/youtube_cookies.txt\n"
    "3. 檢查：python scripts/check_yt_cookies_freshness.py\n"
    "處理完重啟 bot。"
)


class YtBlockAlertMixin:
    def _yt_block_should_alert(self, now: float | None = None) -> bool:
        """記一次秒跳；視窗內達門檻且不在 debounce 內 → True（並開始 debounce）。"""
        now = time.time() if now is None else now
        fails = getattr(self, "_yt_fail_ts", None)
        if fails is None:
            fails = self._yt_fail_ts = deque()
        fails.append(now)
        while fails and now - fails[0] > YT_BLOCK_WINDOW_S:
            fails.popleft()
        if len(fails) < YT_BLOCK_MIN_FAILS:
            return False
        if now - getattr(self, "_yt_alert_ts", float("-inf")) < YT_BLOCK_ALERT_DEBOUNCE_S:
            return False
        self._yt_alert_ts = now
        return True

    def _note_instant_eof(self, title: str, played_s: float, now: float | None = None) -> None:
        """歌只播一瞬、準備 403 重試時呼叫：照舊記 log，並在連續發生時 DM owner。"""
        logger.info(f"🔁 [Stream] 點的歌只播 {played_s:.1f}s，疑似 403，重抓網址重試：{title}")
        if not self._yt_block_should_alert(now):
            return
        logger.critical("🚨 [YouTube 擋線] 連續秒跳，DM owner 更新 cookies")
        text = ALERT_TEXT.format(
            n=len(self._yt_fail_ts), w=YT_BLOCK_WINDOW_S, title=title)

        async def _send() -> None:
            try:
                from owner_auth import owner_id
                oid = owner_id()
                owner = self.bot.get_user(oid) or await self.bot.fetch_user(oid)
                if owner is not None:
                    await owner.send(text[:1990])
            except Exception:
                logger.warning("[YouTube 擋線] DM owner 失敗（忽略）", exc_info=True)

        try:
            asyncio.create_task(_send())
        except RuntimeError:
            pass  # 無 running loop
