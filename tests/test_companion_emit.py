"""
test_companion_emit.py — TDD for C1+C2

測試 companion_bridge 的兩個新 emit 方法，以及 DiscordTemperatureMonitor 的整合。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── 測試 1：emit_temperature_update 廣播正確 payload ──────────────────────────

@pytest.mark.asyncio
async def test_emit_temperature_update_broadcasts_correct_payload():
    """emit_temperature_update("cold", 0.3) 應廣播 level="cold", value=0.3。"""
    from marvin_voice_core.companion_bridge import CompanionBridge

    bridge = CompanionBridge.__new__(CompanionBridge)
    bridge._broadcast = AsyncMock()

    await bridge.emit_temperature_update("cold", 0.3)

    bridge._broadcast.assert_called_once()
    event = bridge._broadcast.call_args[0][0]
    assert event["type"] == "temperature_update"
    assert event["payload"]["level"] == "cold"
    assert event["payload"]["value"] == pytest.approx(0.3, abs=1e-6)


@pytest.mark.asyncio
async def test_emit_temperature_update_rounds_value():
    """emit_temperature_update 的 value 應四捨五入到三位小數。"""
    from marvin_voice_core.companion_bridge import CompanionBridge

    bridge = CompanionBridge.__new__(CompanionBridge)
    bridge._broadcast = AsyncMock()

    await bridge.emit_temperature_update("warm", 1.23456789)

    event = bridge._broadcast.call_args[0][0]
    assert event["payload"]["value"] == pytest.approx(1.235, abs=1e-6)


# ── 測試 2：emit_topic_generated 廣播正確 payload ─────────────────────────────

@pytest.mark.asyncio
async def test_emit_topic_generated_broadcasts_correct_payload():
    """emit_topic_generated(["話題1", "話題2"], "auto") 應廣播 topics 和 trigger。"""
    from marvin_voice_core.companion_bridge import CompanionBridge

    bridge = CompanionBridge.__new__(CompanionBridge)
    bridge._broadcast = AsyncMock()

    await bridge.emit_topic_generated(["話題1", "話題2"], "auto")

    bridge._broadcast.assert_called_once()
    event = bridge._broadcast.call_args[0][0]
    assert event["type"] == "topic_generated"
    assert event["payload"]["topics"] == ["話題1", "話題2"]
    assert event["payload"]["trigger"] == "auto"


@pytest.mark.asyncio
async def test_emit_topic_generated_manual_trigger():
    """trigger="manual" 時 payload 應包含 manual。"""
    from marvin_voice_core.companion_bridge import CompanionBridge

    bridge = CompanionBridge.__new__(CompanionBridge)
    bridge._broadcast = AsyncMock()

    await bridge.emit_topic_generated(["話題A"], "manual")

    event = bridge._broadcast.call_args[0][0]
    assert event["payload"]["trigger"] == "manual"


# ── 測試 3：companion_bridge=None 時 check_and_trigger 不 crash ───────────────

@pytest.mark.asyncio
async def test_temperature_monitor_no_bridge_does_not_crash():
    """companion_bridge=None 時 check_and_trigger() 不應拋例外。"""
    from discord_temperature_monitor import DiscordTemperatureMonitor

    topic_generator_fn = AsyncMock(return_value=[])
    monitor = DiscordTemperatureMonitor(
        topic_generator_fn=topic_generator_fn,
        companion_bridge=None,
    )

    # 確保不丟例外
    await monitor.check_and_trigger()


# ── 測試 4：有 companion_bridge → emit_temperature_update 被呼叫 ──────────────

@pytest.mark.asyncio
async def test_temperature_monitor_emits_on_check():
    """check_and_trigger() 呼叫後應呼叫 companion_bridge.emit_temperature_update。"""
    from discord_temperature_monitor import DiscordTemperatureMonitor

    topic_generator_fn = AsyncMock(return_value=[])

    bridge = MagicMock()
    bridge.emit_temperature_update = AsyncMock()

    monitor = DiscordTemperatureMonitor(
        topic_generator_fn=topic_generator_fn,
        companion_bridge=bridge,
    )

    await monitor.check_and_trigger()

    # emit_temperature_update 必須被呼叫，且 level/value 正確
    bridge.emit_temperature_update.assert_called_once()
    args = bridge.emit_temperature_update.call_args[0]
    assert args[0] in ("cold", "warm", "hot")
    assert isinstance(args[1], float)


# ── 測試 5：冷場直接觸發話題後 → emit_topic_generated("auto") ─────────────────

@pytest.mark.asyncio
async def test_temperature_monitor_emits_topic_on_cold_trigger():
    """冷場 3 分鐘直接觸發話題後，companion_bridge.emit_topic_generated 應以 trigger='auto' 被呼叫。"""
    from discord_temperature_monitor import DiscordTemperatureMonitor

    topics_result = ["話題X", "話題Y"]
    topic_generator_fn = AsyncMock(return_value=topics_result)

    bridge = MagicMock()
    bridge.emit_topic_generated = AsyncMock()
    bridge.emit_temperature_update = AsyncMock()

    monitor = DiscordTemperatureMonitor(
        topic_generator_fn=topic_generator_fn,
        companion_bridge=bridge,
    )

    # 3 次 cold check → 直接觸發話題（不問）
    with patch("discord_temperature_monitor.time.time", return_value=3000.0):
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()
        await monitor.check_and_trigger()

    # ensure_future 是同步的，需要等 loop 執行一次
    await asyncio.sleep(0)

    bridge.emit_topic_generated.assert_called_once()
    call_kwargs = bridge.emit_topic_generated.call_args
    # 可能是 positional 或 keyword arg
    topics_arg = call_kwargs[0][0] if call_kwargs[0] else call_kwargs[1].get("topics")
    trigger_arg = call_kwargs[0][1] if len(call_kwargs[0]) > 1 else call_kwargs[1].get("trigger")
    assert trigger_arg == "auto"
