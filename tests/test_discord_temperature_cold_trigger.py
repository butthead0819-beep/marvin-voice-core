"""
TDD tests for DiscordTemperatureMonitor.

2026-06-01 行為變更：冷場觸發改「直接講話題」，不再先問「要我出個話題嗎？」。
確認機制（_pending_confirm / on_stt_result / wake window / tts_fn ask）整套移除。

覆蓋範圍：
- 溫度計算（空視窗、語音事件、文字事件、加權）
- COLD 連續偵測（3 分鐘才觸發）
- Cooldown（10 分鐘）
- Session cap（每 session 最多 3 次）
- 觸發即直接呼叫 topic_generator_fn（不問、不等確認）
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

pytest_plugins = ["pytest_asyncio"]


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

def _make_monitor(fake_time: float = 1000.0):
    """建立 DiscordTemperatureMonitor，注入 mock topic_generator_fn。

    topic_generator_fn 是 async callable () -> list[str]，自身負責 TTS 播放
    （直接講話題，monitor 不再經 tts_fn 詢問）。
    """
    from discord_temperature_monitor import DiscordTemperatureMonitor

    topic_generator_fn = AsyncMock(return_value=["話題A", "話題B", "話題C"])
    monitor = DiscordTemperatureMonitor(topic_generator_fn=topic_generator_fn)
    return monitor, topic_generator_fn


# ═══════════════════════════════════════════════════════════════════════════════
# 溫度計算
# ═══════════════════════════════════════════════════════════════════════════════

def test_empty_window_is_cold():
    with patch("discord_temperature_monitor.time.time", return_value=1000.0):
        monitor, _ = _make_monitor()
        assert monitor.temperature == 0.0
        assert monitor.level == "cold"


def test_voice_events_raise_temperature():
    with patch("discord_temperature_monitor.time.time", return_value=1000.0):
        monitor, _ = _make_monitor()
        for i in range(5):
            monitor.record_voice_event(user_id=f"user_{i}")
        assert monitor.temperature > 0.0


def test_text_events_raise_temperature():
    with patch("discord_temperature_monitor.time.time", return_value=1000.0):
        monitor, _ = _make_monitor()
        for i in range(5):
            monitor.record_message_event(channel_id="ch1")
        assert monitor.temperature > 0.0


def test_combined_weight():
    """voice 0.6 weight, text 0.4 weight：同等事件數下 voice 溫度貢獻較高"""
    with patch("discord_temperature_monitor.time.time", return_value=1000.0):
        monitor_voice, _ = _make_monitor()
        monitor_text, _ = _make_monitor()
        for i in range(10):
            monitor_voice.record_voice_event(user_id=f"u{i}")
        for i in range(10):
            monitor_text.record_message_event(channel_id="ch1")
        assert monitor_voice.temperature > monitor_text.temperature


def test_temperature_levels():
    with patch("discord_temperature_monitor.time.time", return_value=1000.0):
        monitor, _ = _make_monitor()
        assert monitor.level == "cold"
        for i in range(5):
            monitor.record_voice_event(user_id=f"u{i}")
        assert monitor.level == "warm"


# ═══════════════════════════════════════════════════════════════════════════════
# COLD 連續偵測 — 觸發即「直接講」（topic_generator_fn），不問
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_no_trigger_if_not_three_consecutive_cold_minutes():
    monitor, topic_fn = _make_monitor()
    with patch("discord_temperature_monitor.time.time", return_value=1000.0):
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()
    topic_fn.assert_not_called()


@pytest.mark.asyncio
async def test_trigger_after_three_cold_minutes_speaks_topic_directly():
    """3 分鐘連續 COLD → 直接呼叫 topic_generator_fn（不問是否要話題）。"""
    monitor, topic_fn = _make_monitor()
    with patch("discord_temperature_monitor.time.time", return_value=1000.0):
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()  # 3rd → 直接講
    topic_fn.assert_awaited_once()


@pytest.mark.asyncio
async def test_cold_streak_resets_on_warm():
    monitor, topic_fn = _make_monitor()
    with patch("discord_temperature_monitor.time.time", return_value=1000.0):
        await monitor.check_and_trigger()
        for i in range(5):
            monitor.record_voice_event(user_id=f"u{i}")
        await monitor.check_and_trigger()  # warm → reset
        monitor._msg_times.clear()
        monitor._voice_times.clear()
        await monitor.check_and_trigger()  # 1st cold again
    topic_fn.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# Cooldown
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_no_trigger_within_cooldown():
    monitor, topic_fn = _make_monitor()
    with patch("discord_temperature_monitor.time.time", return_value=1000.0):
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()
    with patch("discord_temperature_monitor.time.time", return_value=1000.0 + 5 * 60):
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()
    assert topic_fn.await_count == 1


@pytest.mark.asyncio
async def test_trigger_after_cooldown_expires():
    monitor, topic_fn = _make_monitor()
    with patch("discord_temperature_monitor.time.time", return_value=1000.0):
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()
    with patch("discord_temperature_monitor.time.time", return_value=1000.0 + 11 * 60):
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()
    assert topic_fn.await_count == 2


# ═══════════════════════════════════════════════════════════════════════════════
# Session cap
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_session_cap_three_times():
    monitor, topic_fn = _make_monitor()
    base_time = 1000.0
    for trigger_idx in range(4):
        t = base_time + trigger_idx * 11 * 60
        with patch("discord_temperature_monitor.time.time", return_value=t):
            await monitor.check_and_trigger()
            await monitor.check_and_trigger()
            await monitor.check_and_trigger()
    assert topic_fn.await_count == 3


@pytest.mark.asyncio
async def test_reset_session_resets_count():
    monitor, topic_fn = _make_monitor()
    base_time = 1000.0
    for trigger_idx in range(3):
        t = base_time + trigger_idx * 11 * 60
        with patch("discord_temperature_monitor.time.time", return_value=t):
            await monitor.check_and_trigger()
            await monitor.check_and_trigger()
            await monitor.check_and_trigger()
    assert topic_fn.await_count == 3

    monitor.reset_session()
    t = base_time + 4 * 11 * 60
    with patch("discord_temperature_monitor.time.time", return_value=t):
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()
    assert topic_fn.await_count == 4


# ═══════════════════════════════════════════════════════════════════════════════
# 確認機制已移除 — monitor 不應再有 on_stt_result / pending confirm
# ═══════════════════════════════════════════════════════════════════════════════

def test_no_confirmation_machinery():
    """確認流程已移除：不再有 on_stt_result method 或 _pending_confirm 狀態。"""
    monitor, _ = _make_monitor()
    assert not hasattr(monitor, "on_stt_result"), "on_stt_result 應已移除（改直接講）"
    assert not hasattr(monitor, "_pending_confirm"), "_pending_confirm 狀態應已移除"
