"""TDD：cogs/music_cog.py::MusicCog._fire_puck_crossfade() 輪詢 /puck/status
取代固定 sleep(buffer_s)。

背景：pi_bt 接上 YouTube cookies 後，Pi 端 resolve_stream_url() 的 deno JS
challenge 常吃到 ~24s CPU time（見該函式 2026-08-18 docstring），遠超原本
假設的 ~7s。固定 sleep(buffer_s) 賭一個時長——猜太短會在 deck_b 還沒 ready
時打 /puck/crossfade，Pi 端 raise RuntimeError 被吞掉，這次轉場直接放棄、
當前曲播完只剩靜音。改成輪詢 next_queued 是否等於 next_url，ready 就提早
出手；client 沒有 status()（esp32_edge_mix）保留舊的固定 sleep 行為。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.music_cog import MusicCog


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    return MusicCog(bot)


@pytest.mark.asyncio
async def test_crossfade_polls_status_and_fires_as_soon_as_deck_b_ready():
    """client 有 status()（pi_bt）→ 不死等 buffer_s，deck_b ready 就提早 crossfade。"""
    cog = _make_cog()
    fake_client = MagicMock(spec=["play", "queue_next", "crossfade", "stop", "status"])
    fake_client.queue_next = AsyncMock(return_value=True)
    fake_client.crossfade = AsyncMock(return_value=True)
    # 前兩次輪詢 next_queued 還沒對上（模擬還在 resolve），第三次才 ready。
    fake_client.status = AsyncMock(side_effect=[
        {"playing": "https://ex/cur", "next_queued": None, "crossfading": False},
        {"playing": "https://ex/cur", "next_queued": None, "crossfading": False},
        {"playing": "https://ex/cur", "next_queued": "https://ex/next", "crossfading": False},
    ])

    with patch("asyncio.sleep", new=AsyncMock()):
        await cog._fire_puck_crossfade(fake_client, "https://ex/next", buffer_s=30.0)

    fake_client.queue_next.assert_awaited_once_with("https://ex/next", title=None)
    assert fake_client.status.await_count == 3
    fake_client.crossfade.assert_awaited_once_with(4.0)


@pytest.mark.asyncio
async def test_crossfade_falls_back_to_fixed_sleep_when_client_lacks_status():
    """esp32_edge_mix 的 client 沒有 status() → 行為不變，固定 sleep(buffer_s) 後才 crossfade。"""
    cog = _make_cog()
    fake_client = MagicMock(spec=["play", "queue_next", "crossfade", "stop"])
    fake_client.queue_next = AsyncMock(return_value=True)
    fake_client.crossfade = AsyncMock(return_value=True)

    with patch("asyncio.sleep", new=AsyncMock()) as fake_sleep:
        await cog._fire_puck_crossfade(fake_client, "https://ex/next", buffer_s=4.0)

    fake_sleep.assert_awaited_once_with(4.0)
    fake_client.crossfade.assert_awaited_once_with(4.0)


@pytest.mark.asyncio
async def test_crossfade_stops_polling_at_buffer_s_deadline_and_still_attempts():
    """轉場慢到超過 buffer_s（deck_b 一直沒 ready）→ 輪詢到上限就放棄等待，
    仍照舊打一次 crossfade（維持舊行為：不會因為輪詢就完全不出手），只是
    這次大概率會被 Pi 端拒絕——呼叫端只記警告，不拋例外。"""
    cog = _make_cog()
    fake_client = MagicMock(spec=["play", "queue_next", "crossfade", "stop", "status"])
    fake_client.queue_next = AsyncMock(return_value=True)
    fake_client.crossfade = AsyncMock(return_value=False)  # Pi 端拒絕（deck_b is None）
    fake_client.status = AsyncMock(return_value={"next_queued": None})

    real_time = __import__("time").time
    calls = {"n": 0}

    def _fake_time():
        # 前幾次呼叫落在 deadline 內，之後直接跳過 deadline，避免測試真的跑很久。
        calls["n"] += 1
        return real_time() if calls["n"] <= 2 else real_time() + 100.0

    with patch("asyncio.sleep", new=AsyncMock()), \
         patch("cogs.music_cog.time.time", side_effect=_fake_time):
        await cog._fire_puck_crossfade(fake_client, "https://ex/next", buffer_s=2.0)

    fake_client.crossfade.assert_awaited_once_with(4.0)
