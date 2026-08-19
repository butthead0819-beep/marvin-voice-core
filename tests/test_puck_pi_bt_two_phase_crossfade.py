"""TDD：cogs/music_cog.py::MusicCog._run_puck_pi_bt_crossfade() 兩階段排程。

2026-08-19 重寫背景：LEAD_S 這一顆常數同時決定「多早開始 queue_next」跟
「什麼時候真的 crossfade」，兩個目標互相打架——resolve 快就提早結束、
resolve 慢就撞真正結尾被拒絕。改成兩階段：PREFETCH（早，只 queue_next，
不影響聽感）→ FIRE（近真正結尾，短暫輪詢 ready 就 crossfade，逾時就放棄）。
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    from cogs.music_cog import MusicCog
    return MusicCog(bot)


def _cur_info(duration=180.0):
    return {"title": "周杰倫 - 夜曲", "url": "https://ex/cur", "duration": duration}


def _next_info():
    return {"title": "陶喆 - 普通朋友", "url": "https://ex/next", "webpage_url": "https://ex/next"}


def _prime(cog, cur):
    cog._current_stream_info = cur
    cog._current_song_skipped = False
    cog.stream_mode = True


@pytest.mark.asyncio
async def test_queue_next_fires_at_prefetch_point_crossfade_fires_later_when_ready():
    """queue_next 只在 PREFETCH 點打一次；FIRE 點輪詢立刻命中 ready → crossfade 一次。"""
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()
    cog.stream_queue = [nxt]
    _prime(cog, cur)

    fake_client = MagicMock()
    fake_client.queue_next = AsyncMock(return_value=True)
    fake_client.status = AsyncMock(return_value={"next_queued": nxt["webpage_url"]})
    fake_client.crossfade = AsyncMock(return_value=True)

    with patch("cogs.music_cog._get_puck_client", return_value=fake_client), \
         patch("asyncio.sleep", new=AsyncMock()):
        await cog._run_puck_pi_bt_crossfade(cur, time.time() - 10.0)

    fake_client.queue_next.assert_awaited_once_with(nxt["webpage_url"], title=nxt["title"])
    fake_client.crossfade.assert_awaited_once_with(4.0)
    assert nxt["_puck_pi_bt_handed_off"] is True


@pytest.mark.asyncio
async def test_gives_up_without_crossfade_when_not_ready_at_fire_deadline():
    """FIRE 點輪詢到期仍未 ready → 不呼叫 crossfade，標記 handed_off=False（交給
    下一首開頭補硬 play 的既有回退機制）。"""
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()
    cog.stream_queue = [nxt]
    _prime(cog, cur)

    fake_client = MagicMock()
    fake_client.queue_next = AsyncMock(return_value=True)
    fake_client.status = AsyncMock(return_value={"next_queued": None})
    fake_client.crossfade = AsyncMock(return_value=True)

    real_time = time.time
    calls = {"n": 0}

    def _fake_time():
        calls["n"] += 1
        return real_time() if calls["n"] <= 2 else real_time() + 100.0

    with patch("cogs.music_cog._get_puck_client", return_value=fake_client), \
         patch("asyncio.sleep", new=AsyncMock()), \
         patch("cogs.music_cog.time.time", side_effect=_fake_time):
        await cog._run_puck_pi_bt_crossfade(cur, time.time() - 10.0)

    fake_client.crossfade.assert_not_awaited()
    assert nxt["_puck_pi_bt_handed_off"] is False


@pytest.mark.asyncio
async def test_queue_next_failure_marks_not_handed_off_without_polling():
    """PREFETCH 點 queue_next 就失敗 → 直接標記 False，不進 FIRE 輪詢。"""
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()
    cog.stream_queue = [nxt]
    _prime(cog, cur)

    fake_client = MagicMock()
    fake_client.queue_next = AsyncMock(return_value=False)
    fake_client.status = AsyncMock()
    fake_client.crossfade = AsyncMock(return_value=True)

    with patch("cogs.music_cog._get_puck_client", return_value=fake_client), \
         patch("asyncio.sleep", new=AsyncMock()):
        await cog._run_puck_pi_bt_crossfade(cur, time.time() - 10.0)

    fake_client.status.assert_not_awaited()
    fake_client.crossfade.assert_not_awaited()
    assert nxt["_puck_pi_bt_handed_off"] is False
