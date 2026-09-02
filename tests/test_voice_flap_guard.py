"""☢️ [Voice Flap Guard] 語音連線反覆「連上→斷線」→ 放棄軟修復、物理重啟。

2026-09-02 事故：CryptoError storm 後語音 WS 每 ~60s 斷一次連 15+ 分。discord.py 內部
自動重連在兩次 60s 巡邏之間就接回來 → sentinel_monitor_loop 輪詢一次也數不到（review P1）。
改由 _VoiceFlapObserver 從 discord.voice_state log 抓「連上→斷線」轉換；單一場持續斷線
只算一次（review P2）。
"""
from __future__ import annotations

import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.voice_controller_connection import _VoiceFlapObserver


def _rec(msg: str) -> logging.LogRecord:
    return logging.LogRecord("discord.voice_state", logging.INFO, __file__, 0, msg, None, None)


# ── _VoiceFlapObserver：只數「連上→斷線」轉換 ──────────────────────────────────

def test_observer_counts_connect_then_disconnect():
    hits = []
    obs = _VoiceFlapObserver(lambda: hits.append(1))
    obs.emit(_rec("Voice connection complete."))
    obs.emit(_rec("Disconnected from voice... Reconnecting in 0.09s."))
    assert hits == [1]


def test_observer_ignores_disconnect_before_any_connect():
    hits = []
    obs = _VoiceFlapObserver(lambda: hits.append(1))
    obs.emit(_rec("Disconnected from voice... Reconnecting in 1.0s."))
    assert hits == []


def test_observer_sustained_outage_counts_once():
    """一場持續斷線：連上一次後 discord.py backoff 狂印 Disconnected → 只算 1。"""
    hits = []
    obs = _VoiceFlapObserver(lambda: hits.append(1))
    obs.emit(_rec("Voice connection complete."))
    for _ in range(6):
        obs.emit(_rec("Disconnected from voice... Reconnecting in 8.0s."))
        obs.emit(_rec("Could not connect to voice... Retrying..."))
    assert hits == [1]


def test_observer_real_flap_counts_each_cycle():
    hits = []
    obs = _VoiceFlapObserver(lambda: hits.append(1))
    for _ in range(5):
        obs.emit(_rec("Voice connection complete."))
        obs.emit(_rec("Disconnected from voice... Reconnecting in 0.1s."))
    assert len(hits) == 5


# ── _record_voice_flap：窗內達門檻 → self_restart（force 不傳，靠 900s 防抖）────

def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.router = MagicMock()
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.post_summon_callback = None
    with patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        cog = VoiceController(bot)
    cog.self_restart = AsyncMock()
    return cog


@pytest.mark.asyncio
async def test_record_flap_hard_restarts_at_threshold():
    cog = _make_cog()
    now = time.time()
    cog._voice_flap_ts.extend([now - 40, now - 30, now - 20, now - 10])  # 已 4
    cog._record_voice_flap()  # 第 5
    cog.self_restart.assert_called_once()
    assert "force" not in cog.self_restart.call_args.kwargs  # 靠既有 900s 防抖，不 force


@pytest.mark.asyncio
async def test_record_flap_below_threshold_no_restart():
    cog = _make_cog()
    cog._voice_flap_ts.append(time.time() - 5)
    cog._record_voice_flap()  # 才第 2
    cog.self_restart.assert_not_called()


@pytest.mark.asyncio
async def test_record_flap_stale_events_outside_window_dont_count():
    cog = _make_cog()
    now = time.time()
    cog._voice_flap_ts.extend([now - 900, now - 800, now - 700, now - 600])  # 窗外
    cog._record_voice_flap()
    cog.self_restart.assert_not_called()


def test_install_watch_is_idempotent():
    cog = _make_cog()
    lg = logging.getLogger("discord.voice_state")
    before = list(lg.handlers)
    try:
        cog._install_voice_flap_watch()
        cog._install_voice_flap_watch()
        added = [h for h in lg.handlers if isinstance(h, _VoiceFlapObserver)]
        assert len(added) == 1
    finally:
        for h in list(lg.handlers):
            if isinstance(h, _VoiceFlapObserver):
                lg.removeHandler(h)
        assert list(lg.handlers) == before
