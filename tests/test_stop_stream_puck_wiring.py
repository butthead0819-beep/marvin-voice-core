"""TDD：stop_stream() 從沒通知車puck端（pi_bt/esp32_edge_mix）停播——2026-08-17
實機踩到：使用者語音「停止播放」後，Mac 端 stream_mode 正確歸位，但 Pi 端
`/puck/status` 仍卡在舊的 `playing: <url>`，deck 繼續跑。下次 /puck/play 送新歌
時 Pi 端狀態可能仍殘留舊 deck，也讓「兩首歌之間等很久」更難排查（無法用
/puck/status 判斷 Mac 到底有沒有真的下過指令）。修法：stop_stream() 比照
_fire_puck_play/_fire_puck_crossfade 的既有慣例，一併呼叫 puck_client.stop()。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="key")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="深夜")

    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    cog.stream_mode = True
    return cog


@pytest.mark.asyncio
async def test_stop_stream_fires_puck_stop_when_client_configured():
    cog = _make_cog()
    fake_client = MagicMock()
    fake_client.stop = AsyncMock(return_value=True)

    with patch("cogs.music_cog._get_puck_client", return_value=fake_client):
        await cog.stop_stream(reason="測試")
        await asyncio.sleep(0)   # 讓 create_task 起的背景呼叫真的跑

    fake_client.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_stream_skips_puck_stop_when_no_client():
    """非車puck硬體（家用 Pi 3B 等）→ _get_puck_client() 回 None，不該呼叫也不該炸。"""
    cog = _make_cog()

    with patch("cogs.music_cog._get_puck_client", return_value=None):
        await cog.stop_stream(reason="測試")
        await asyncio.sleep(0)
    # 沒拋例外就是過。


@pytest.mark.asyncio
async def test_stop_stream_does_not_block_on_puck_stop_failure():
    """puck_client.stop() 失敗（連不到 Pi）不該讓 stop_stream() 本身炸掉——
    Mac 端的停播（stream_mode 歸位等）優先，裝置端通知是盡力而為。"""
    cog = _make_cog()
    fake_client = MagicMock()
    fake_client.stop = AsyncMock(side_effect=RuntimeError("連不到 Pi"))

    with patch("cogs.music_cog._get_puck_client", return_value=fake_client):
        await cog.stop_stream(reason="測試")
        await asyncio.sleep(0)
    # 沒拋例外就是過；stream_mode 該已經歸位。
    assert cog.stream_mode is False
