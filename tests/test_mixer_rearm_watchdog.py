"""☢️ [Mixer Rearm Watchdog] 觀測防線：mixer 重武裝頻率滑動視窗超門檻就 CRITICAL 告警。

2026-09-17 事故（見 tests/test_mixer_rearm_storm.py）花 17 小時才查到真根因，因為當時
沒有任何東西在數「重武裝頻率」——只能翻 log 數 `adapter armed` 出現次數。這裡在
_ensure_mixer_playing 真的執行了重武裝的單一 choke point（_record_mixer_rearm）計數，
超門檻 logger.critical（既有 ErrorDispatcher 會接走 DM owner）。純觀測，不做任何自動
修復：自癒本身已有 idle 不重武裝 + 最小間隔防抖兩道防線（見 f518c97），這裡不跟它們
打架，也不重複告警刷爆 DM（同一波風暴 5 分鐘內只叫一次）。
"""
from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock, patch

import pytest


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
    return cog


def test_normal_frequency_no_alert(caplog):
    """60 秒內只有 3 次重武裝（正常個位數）→ 不告警。"""
    cog = _make_cog()
    now = time.time()
    for offset in (40, 30, 20):
        cog._mixer_rearm_ts.append(now - offset)
    with caplog.at_level(logging.CRITICAL, logger="cogs.voice_controller_playback"):
        cog._record_mixer_rearm()
    assert not any(r.levelno == logging.CRITICAL for r in caplog.records)


def test_over_threshold_alerts_once(caplog):
    """超過門檻（20/分鐘）→ 告警一次。"""
    cog = _make_cog()
    now = time.time()
    for offset in range(19):
        cog._mixer_rearm_ts.append(now - offset)
    with caplog.at_level(logging.CRITICAL, logger="cogs.voice_controller_playback"):
        cog._record_mixer_rearm()  # 第 20 次
    criticals = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(criticals) == 1
    assert "Mixer Rearm Watchdog" in criticals[0].message


def test_repeated_storm_within_debounce_not_repeated(caplog):
    """同一波風暴（連續超標）5 分鐘防抖內不重複告警。"""
    cog = _make_cog()
    now = time.time()
    for offset in range(19):
        cog._mixer_rearm_ts.append(now - offset)
    with caplog.at_level(logging.CRITICAL, logger="cogs.voice_controller_playback"):
        cog._record_mixer_rearm()  # 第 20 次 → 告警
        cog._record_mixer_rearm()  # 第 21 次 → 仍超標但防抖擋掉
        cog._record_mixer_rearm()  # 第 22 次 → 仍擋掉
    criticals = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(criticals) == 1


def test_alert_again_after_debounce_window(caplog):
    """防抖視窗過後（風暴仍在燒）可以再告警一次。"""
    cog = _make_cog()
    now = time.time()
    for offset in range(19):
        cog._mixer_rearm_ts.append(now - offset)
    with caplog.at_level(logging.CRITICAL, logger="cogs.voice_controller_playback"):
        cog._record_mixer_rearm()  # 第 20 次 → 告警
        cog._mixer_rearm_alert_ts -= (cog._MIXER_REARM_WATCHDOG_ALERT_DEBOUNCE_S + 1.0)  # 時間快轉
        cog._record_mixer_rearm()  # 仍超標且防抖已過期 → 再告警
    criticals = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(criticals) == 2


def test_stale_events_outside_window_dont_count(caplog):
    """視窗外的舊事件不計入（滑動視窗正確性）。"""
    cog = _make_cog()
    now = time.time()
    for offset in range(19):
        cog._mixer_rearm_ts.append(now - 300 - offset)  # 全部在 60s 視窗外
    with caplog.at_level(logging.CRITICAL, logger="cogs.voice_controller_playback"):
        cog._record_mixer_rearm()  # 只有這一筆在視窗內
    assert not any(r.levelno == logging.CRITICAL for r in caplog.records)


# ── 接線守門：上面 5 個測試都直接呼叫 _record_mixer_rearm()，若有人日後把
# _ensure_mixer_playing 裡的呼叫點刪掉，那 5 個測試依然全綠、看門狗卻已經死了。
# 這個專案踩過同型的坑（wire code ≠ 真的啟用），所以補測真實接線。

def _plan12_cog(monkeypatch):
    monkeypatch.setenv("PLAN12_LOCAL_MIX", "true")
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.get_estimated_duration.return_value = 2.0
    with patch("discord_voice_engine.faster_whisper", None, create=True):
        from discord_voice_engine import DiscordVoiceEngine
        bot.engine = DiscordVoiceEngine(bot)
    with patch("discord.ext.tasks.loop", lambda *a, **kw: lambda f: f), \
         patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        return VoiceController(bot)


def test_real_arm_is_counted_by_watchdog(monkeypatch):
    """真的 arm 成功 → 計數器要真的被推進（證明 choke point 有接上）。"""
    from marvin_voice_core.playback_device import DiscordPlaybackDevice
    cog = _plan12_cog(monkeypatch)
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = False
    assert len(cog._mixer_rearm_ts) == 0
    assert cog._ensure_mixer_playing(DiscordPlaybackDevice(vc)) is True
    assert len(cog._mixer_rearm_ts) == 1


def test_noop_arm_is_not_counted(monkeypatch):
    """已經在播 → ensure 回 False（沒真的 arm）→ 不可計數，否則門檻會被灌水誤報。"""
    from marvin_voice_core.playback_device import DiscordPlaybackDevice
    cog = _plan12_cog(monkeypatch)
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = True  # 已在播
    assert cog._ensure_mixer_playing(DiscordPlaybackDevice(vc)) is False
    assert len(cog._mixer_rearm_ts) == 0
