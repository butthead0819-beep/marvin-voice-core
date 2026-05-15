"""
TDD tests for DiscordTemperatureMonitor.

覆蓋範圍：
- 溫度計算（空視窗、語音事件、文字事件、加權）
- COLD 連續偵測（3 分鐘才觸發）
- Cooldown（10 分鐘）
- Session cap（每 session 最多 3 次）
- ConfirmationContext（肯定/否定/無 pending）
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── 為了讓測試可以 import，還未存在的模組先跳過 ──────────────────────────────
pytest_plugins = ["pytest_asyncio"]


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

def _make_monitor(fake_time: float = 1000.0):
    """建立 DiscordTemperatureMonitor，注入 mock 依賴。

    topic_generator_fn 是 async callable () -> list[str]，由 caller 封裝
    voice_members / guild_id 的取得邏輯。
    """
    from discord_temperature_monitor import DiscordTemperatureMonitor

    wake_detector = MagicMock()
    wake_detector.temporary_open_window = MagicMock()

    tts_fn = AsyncMock()

    topic_generator_fn = AsyncMock(return_value=["話題A", "話題B", "話題C"])

    monitor = DiscordTemperatureMonitor(
        wake_detector=wake_detector,
        tts_fn=tts_fn,
        topic_generator_fn=topic_generator_fn,
    )
    return monitor, wake_detector, tts_fn, topic_generator_fn


# ═══════════════════════════════════════════════════════════════════════════════
# 溫度計算
# ═══════════════════════════════════════════════════════════════════════════════

def test_empty_window_is_cold():
    """無事件 → temperature=0.0 → level='cold'"""
    with patch("discord_temperature_monitor.time.time", return_value=1000.0):
        monitor, *_ = _make_monitor()
        assert monitor.temperature == 0.0
        assert monitor.level == "cold"


def test_voice_events_raise_temperature():
    """加 5 個語音事件 → temperature > 0"""
    with patch("discord_temperature_monitor.time.time", return_value=1000.0):
        monitor, *_ = _make_monitor()
        for i in range(5):
            monitor.record_voice_event(user_id=f"user_{i}")
        assert monitor.temperature > 0.0


def test_text_events_raise_temperature():
    """加 5 個文字事件 → temperature > 0"""
    with patch("discord_temperature_monitor.time.time", return_value=1000.0):
        monitor, *_ = _make_monitor()
        for i in range(5):
            monitor.record_message_event(channel_id="ch1")
        assert monitor.temperature > 0.0


def test_combined_weight():
    """voice 0.6 weight, text 0.4 weight：同等事件數下 voice 溫度貢獻較高"""
    with patch("discord_temperature_monitor.time.time", return_value=1000.0):
        monitor_voice, *_ = _make_monitor()
        monitor_text, *_ = _make_monitor()

        for i in range(10):
            monitor_voice.record_voice_event(user_id=f"u{i}")
        for i in range(10):
            monitor_text.record_message_event(channel_id="ch1")

        # voice 貢獻 60%，text 貢獻 40%
        assert monitor_voice.temperature > monitor_text.temperature


def test_temperature_levels():
    """COLD < 0.5, WARM 0.5-2.0, HOT > 2.0"""
    with patch("discord_temperature_monitor.time.time", return_value=1000.0):
        monitor, *_ = _make_monitor()
        assert monitor.level == "cold"  # 0 events → cold

        # 加入足夠事件讓溫度 > 0.5（5 min window，5/5=1.0 voice temp → combined 0.6）
        for i in range(5):
            monitor.record_voice_event(user_id=f"u{i}")
        # 5 events / 5 min = 1.0 voice_temp → combined = 0.6*1.0 = 0.6 → warm
        assert monitor.level == "warm"


# ═══════════════════════════════════════════════════════════════════════════════
# COLD 連續偵測
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_no_trigger_if_not_three_consecutive_cold_minutes():
    """只有 2 分鐘 COLD → 不觸發 TTS"""
    monitor, wake_detector, tts_fn, topic_generator_fn = _make_monitor()

    with patch("discord_temperature_monitor.time.time", return_value=1000.0):
        await monitor.check_and_trigger()  # 1st cold minute
        await monitor.check_and_trigger()  # 2nd cold minute

    tts_fn.assert_not_called()


@pytest.mark.asyncio
async def test_trigger_after_three_consecutive_cold_minutes():
    """3 分鐘連續 COLD → TTS 被呼叫一次"""
    monitor, wake_detector, tts_fn, topic_generator_fn = _make_monitor()

    with patch("discord_temperature_monitor.time.time", return_value=1000.0):
        await monitor.check_and_trigger()  # 1st
        await monitor.check_and_trigger()  # 2nd
        await monitor.check_and_trigger()  # 3rd → trigger

    tts_fn.assert_called_once()


@pytest.mark.asyncio
async def test_cold_streak_resets_on_warm():
    """中間有 WARM 打斷 → cold streak 歸零，不應在第 3 次觸發"""
    monitor, wake_detector, tts_fn, topic_generator_fn = _make_monitor()

    with patch("discord_temperature_monitor.time.time", return_value=1000.0):
        await monitor.check_and_trigger()  # 1st cold
        # 加入事件讓溫度升到 warm
        for i in range(5):
            monitor.record_voice_event(user_id=f"u{i}")
        await monitor.check_and_trigger()  # warm → streak reset
        monitor._msg_times.clear()
        monitor._voice_times.clear()  # 清空事件，重回 cold
        await monitor.check_and_trigger()  # 1st cold (重新計數)

    tts_fn.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# Cooldown
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_no_trigger_within_cooldown():
    """觸發後 5 分鐘內（還在 10 min cooldown）再 check → 不觸發第二次"""
    monitor, wake_detector, tts_fn, topic_generator_fn = _make_monitor()

    with patch("discord_temperature_monitor.time.time", return_value=1000.0):
        # 先觸發一次
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()  # 3rd → trigger (1st)

    # 5 分鐘後（還在 10min cooldown 內）再連續 3 次 check
    with patch("discord_temperature_monitor.time.time", return_value=1000.0 + 5 * 60):
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()

    # 只應呼叫一次（cooldown 中不觸發）
    assert tts_fn.call_count == 1


@pytest.mark.asyncio
async def test_trigger_after_cooldown_expires():
    """觸發後超過 10 分鐘 → cooldown 結束，可再次觸發"""
    monitor, wake_detector, tts_fn, topic_generator_fn = _make_monitor()

    with patch("discord_temperature_monitor.time.time", return_value=1000.0):
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()  # trigger #1

    # 11 分鐘後，冷卻結束
    with patch("discord_temperature_monitor.time.time", return_value=1000.0 + 11 * 60):
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()  # trigger #2

    assert tts_fn.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════════
# Session cap
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_session_cap_three_times():
    """3 次觸發後 → 即使 cooldown 結束也不再觸發"""
    monitor, wake_detector, tts_fn, topic_generator_fn = _make_monitor()

    base_time = 1000.0
    for trigger_idx in range(4):  # 嘗試觸發 4 次
        t = base_time + trigger_idx * 11 * 60  # 每次間隔 11 分鐘（超過 cooldown）
        with patch("discord_temperature_monitor.time.time", return_value=t):
            await monitor.check_and_trigger()
            await monitor.check_and_trigger()
            await monitor.check_and_trigger()

    # 只觸發 3 次（session cap）
    assert tts_fn.call_count == 3


@pytest.mark.asyncio
async def test_reset_session_resets_count():
    """reset_session() 後 → session 計數器歸零，可再次觸發"""
    monitor, wake_detector, tts_fn, topic_generator_fn = _make_monitor()

    base_time = 1000.0
    # 先觸發 3 次（達到 session cap）
    for trigger_idx in range(3):
        t = base_time + trigger_idx * 11 * 60
        with patch("discord_temperature_monitor.time.time", return_value=t):
            await monitor.check_and_trigger()
            await monitor.check_and_trigger()
            await monitor.check_and_trigger()

    assert tts_fn.call_count == 3

    # reset session 後，應可再次觸發
    monitor.reset_session()
    t = base_time + 4 * 11 * 60
    with patch("discord_temperature_monitor.time.time", return_value=t):
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()

    assert tts_fn.call_count == 4  # 第 4 次觸發成功


# ═══════════════════════════════════════════════════════════════════════════════
# ConfirmationContext
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_affirmative_reply_triggers_topic_generator():
    """觸發後的確認視窗內說「要」→ topic_generator.generate_topics 被呼叫"""
    monitor, wake_detector, tts_fn, topic_generator_fn = _make_monitor()

    with patch("discord_temperature_monitor.time.time", return_value=1000.0):
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()  # trigger → pending_confirm = True
        # 模擬語音肯定回覆（在確認視窗內，patch 保持 frozen time）
        monitor.on_stt_result("好啊", user_id="user_jack")

    # 讓 ensure_future 完成
    await asyncio.sleep(0)

    topic_generator_fn.assert_called_once()


@pytest.mark.asyncio
async def test_non_affirmative_does_not_trigger():
    """確認視窗內說「不要」→ topic_generator 不被呼叫"""
    monitor, wake_detector, tts_fn, topic_generator_fn = _make_monitor()

    with patch("discord_temperature_monitor.time.time", return_value=1000.0):
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()
        monitor.on_stt_result("不要", user_id="user_jack")
        await asyncio.sleep(0)

    topic_generator_fn.assert_not_called()


@pytest.mark.asyncio
async def test_stale_pending_confirm_expires_after_window():
    """確認視窗 30 秒過期後，即使說「要」也不觸發 topic generator。"""
    monitor, wake_detector, tts_fn, topic_generator_fn = _make_monitor()

    # 觸發確認視窗（在 t=1000.0 觸發 → 視窗 1000–1030）
    with patch("discord_temperature_monitor.time.time", return_value=1000.0):
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()

    # 31 秒後說「要」→ stale，應被忽略
    with patch("discord_temperature_monitor.time.time", return_value=1031.0):
        monitor.on_stt_result("要", user_id="user_jack")

    await asyncio.sleep(0)
    topic_generator_fn.assert_not_called()


def test_no_confirm_pending_ignores_stt():
    """沒有 pending confirm → on_stt_result 無作用（topic_generator 不呼叫）"""
    monitor, wake_detector, tts_fn, topic_generator_fn = _make_monitor()

    # 從未觸發，pending_confirm = False
    monitor.on_stt_result("好", user_id="user_jack")

    topic_generator_fn.assert_not_called()


@pytest.mark.asyncio
async def test_wake_detector_window_opened_on_trigger():
    """觸發時 wake_detector.temporary_open_window(30, reason='topic_confirm') 被呼叫"""
    monitor, wake_detector, tts_fn, topic_generator_fn = _make_monitor()

    with patch("discord_temperature_monitor.time.time", return_value=1000.0):
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()

    wake_detector.temporary_open_window.assert_called_once_with(30, reason="topic_confirm")


@pytest.mark.asyncio
async def test_confirm_pending_cleared_after_stt_result():
    """肯定回覆後，pending_confirm 清除，不會重複觸發"""
    monitor, wake_detector, tts_fn, topic_generator_fn = _make_monitor()

    with patch("discord_temperature_monitor.time.time", return_value=1000.0):
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()

        monitor.on_stt_result("好", user_id="user_jack")
        await asyncio.sleep(0)

        # 再次呼叫 on_stt_result → pending 已清除，不應再次觸發
        monitor.on_stt_result("好", user_id="user_jack")
        await asyncio.sleep(0)

    assert topic_generator_fn.call_count == 1
