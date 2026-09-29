"""
test_companion_emit.py — DiscordTemperatureMonitor 邊界測試。
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


# ── 測試：check_and_trigger 不 crash ───────────────

@pytest.mark.asyncio
async def test_temperature_monitor_no_bridge_does_not_crash():
    """check_and_trigger() 不應拋例外。"""
    from discord_temperature_monitor import DiscordTemperatureMonitor

    topic_generator_fn = AsyncMock(return_value=[])
    monitor = DiscordTemperatureMonitor(
        topic_generator_fn=topic_generator_fn,
    )

    # 確保不丟例外
    await monitor.check_and_trigger()
