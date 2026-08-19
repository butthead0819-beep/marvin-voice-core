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

    fake_client.queue_next.assert_awaited_once_with(nxt["webpage_url"], title=nxt["title"], seek=None)
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
async def test_highlight_start_s_still_subtracted_from_schedule():
    """2026-08-19：pi_bt 現在真的把 highlight_start_s 當 seek 傳給 Pi（見
    test_queue_next_passes_highlight_start_s_as_seek），Pi 播的內容跟 Discord
    本地一樣是跳過前奏的版本，所以排程算 duration 時該扣掉這段——不能因為
    「Pi 以前沒真的 seek」就永久放棄扣減，兩邊已經對齊了。用同一組
    duration/real_start，有 highlight_start_s 該比沒有時更早觸發 FIRE
    （sleep 到 FIRE 的秒數更短）。"""
    cog = _make_cog()
    nxt = _next_info()
    fake_client = MagicMock()
    fake_client.queue_next = AsyncMock(return_value=True)
    fake_client.status = AsyncMock(return_value={"next_queued": nxt["webpage_url"]})
    fake_client.crossfade = AsyncMock(return_value=True)

    sleeps = {"with": [], "without": []}

    def _make_recorder(label):
        async def _record(delay):
            sleeps[label].append(delay)
        return _record

    for label, cur in [
        ("without", _cur_info(duration=180.0)),
        ("with", {**_cur_info(duration=180.0), "highlight_start_s": 20.0}),
    ]:
        cog.stream_queue = [nxt]
        _prime(cog, cur)
        real_start = time.time() - 10.0
        with patch("cogs.music_cog._get_puck_client", return_value=fake_client), \
             patch("asyncio.sleep", new=AsyncMock(side_effect=_make_recorder(label))):
            await cog._run_puck_pi_bt_crossfade(cur, real_start)

    # duration 少 20s → expected_end_ts 提早 20s → PREFETCH/FIRE 兩個絕對時間
    # 點各自都提早 20s，兩段 sleep 各短 20s，總和少 40s。
    assert sum(sleeps["without"]) - sum(sleeps["with"]) == pytest.approx(40.0, abs=0.2)


@pytest.mark.asyncio
async def test_queue_next_passes_highlight_start_s_as_seek():
    """2026-08-19 實機踩到「提前結束約10秒」：Pi 端一直沒真的套用
    highlight_start_s（YouTube 熱力圖精華起點）跳過前奏，Discord 本地播放
    卻有——Pi 播的內容比 Mac 排程假設的長了這段秒數，長期造成 Pi 提早進入
    尾聲。改成 queue_next() 也把 highlight_start_s 當 seek 傳給 Pi，讓兩邊
    播的是同一個起點。"""
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()
    nxt["highlight_start_s"] = 12.3
    nxt["duration"] = 200.0   # 剩餘 187.7s，遠大於 _safe_pi_bt_seek 的 45s 安全邊界
    cog.stream_queue = [nxt]
    _prime(cog, cur)

    fake_client = MagicMock()
    fake_client.queue_next = AsyncMock(return_value=True)
    fake_client.status = AsyncMock(return_value={"next_queued": nxt["webpage_url"]})
    fake_client.crossfade = AsyncMock(return_value=True)

    with patch("cogs.music_cog._get_puck_client", return_value=fake_client), \
         patch("asyncio.sleep", new=AsyncMock()):
        await cog._run_puck_pi_bt_crossfade(cur, time.time() - 10.0)

    fake_client.queue_next.assert_awaited_once_with(
        nxt["webpage_url"], title=nxt["title"], seek=12.3)


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
