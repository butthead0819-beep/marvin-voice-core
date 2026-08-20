"""TDD：cogs/music_cog.py::MusicCog._fire_puck_crossfade() 依 client 是否有 status()
決定輪詢還是固定 sleep(buffer_s)。

背景：這個能力分派原本是為 pi_bt（Pi mk2）加的——接上 YouTube cookies 後 resolve
常吃到 ~24s CPU time，固定 sleep 賭時長不管用，改成輪詢 next_queued 是否等於
next_url、ready 就提早出手。2026-08-20 起 pi_bt 換歌決策已改走 /audio_stream
「收音機」模式，不再呼叫這支函式（見 main_satellite.py::setup_satellite 說明）；
`_fire_puck_crossfade` 現在只有 esp32_edge_mix 會呼叫，它的 client 沒有 status()，
永遠走下面第二條測試（固定 sleep）的路徑。輪詢分支保留成 hasattr 能力分派（跟
speak/speak_text、sfx 等既有 pattern一致）——這裡繼續測它，確保這條路以後有
client 補上 status() 時邏輯依然正確。"""
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
async def test_crossfade_gives_up_when_still_not_ready_at_buffer_s_deadline():
    """2026-08-19：轉場慢到超過 buffer_s（deck_b 一直沒 ready）→ 輪詢到上限就
    放棄，不再賭一把硬打 crossfade。舊行為（逾時仍硬打）就是「花田錯提早
    結束、~20s 沒聲音」的根因：逼近真正歌曲結尾時硬打常被 Pi 端拒絕
    （RuntimeError: deck_b is None），比乾脆不打、直接讓下一首開頭補
    硬 play（_puck_pi_bt_handed_off=False 那條路）還慢。"""
    cog = _make_cog()
    fake_client = MagicMock(spec=["play", "queue_next", "crossfade", "stop", "status"])
    fake_client.queue_next = AsyncMock(return_value=True)
    fake_client.crossfade = AsyncMock(return_value=False)
    fake_client.status = AsyncMock(return_value={"next_queued": None})

    real_time = __import__("time").time
    calls = {"n": 0}

    def _fake_time():
        # 前幾次呼叫落在 deadline 內，之後直接跳過 deadline，避免測試真的跑很久。
        calls["n"] += 1
        return real_time() if calls["n"] <= 2 else real_time() + 100.0

    with patch("asyncio.sleep", new=AsyncMock()), \
         patch("cogs.music_cog.time.time", side_effect=_fake_time):
        result = await cog._fire_puck_crossfade(fake_client, "https://ex/next", buffer_s=2.0)

    fake_client.crossfade.assert_not_awaited()
    assert result is False
