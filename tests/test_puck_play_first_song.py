"""TDD: esp32_edge_mix 硬體上，開場第一首（沒經過 _fire_puck_crossfade 接手）要送
puck_client.play(webpage_url) 讓 ESP32 從乾淨狀態開播——STEP 11 rollback commit 點出的
缺口：production 只有 _run_tail_dj 會叫 queue_next+crossfade，開場第一首/skip 之後那首
從沒人叫過 play，兩個 deck 永遠閒置沒聲音。見 cogs/music_cog.py::_stream_loop 裡
`if not _dj_played_in_tail:` 那段、_fire_puck_play()。

2026-08-20：pi_bt（Pi Zero 2W 車 puck）換歌決策/DJ口白改回跟家用喇叭共用同一顆
mixer、走 /audio_stream「收音機」模式（見 main_satellite.py::setup_satellite 的
TeeSpeakerOutput 說明）——_get_puck_client() 對 pi_bt 一律回 None，這整條 puck_client
硬 play 邏輯現在只剩 esp32_edge_mix 會走到。pi_bt 的行為完全等同「沒有 puck client」
（見最後一條測試）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None   # _vc() → None，跳過語音頻道相關分支
    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="key")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="深夜")

    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    cog.play_stream_song = AsyncMock()
    # 播完一首後佇列空 → 讓迴圈乾淨結束，不要觸發真的 autopilot/yt-dlp 呼叫。
    cog._auto_recommend = AsyncMock()
    cog._last_resort_replay = AsyncMock(return_value=False)
    return cog


def _done_future(value):
    fut = asyncio.get_event_loop().create_future()
    fut.set_result(value)
    return fut


def _song(webpage_url="https://youtube.com/watch?v=abc123", played_in_tail=False):
    info = {"title": "測試歌", "url": "https://ex/resolved-cdn-url",
            "webpage_url": webpage_url, "requested_by": "狗與露"}
    if played_in_tail:
        info["_dj_played_in_tail"] = True
    return info


@pytest.mark.asyncio
async def test_stream_loop_fires_puck_play_for_song_not_played_in_tail():
    """開場第一首（無 _dj_played_in_tail）→ 送 puck_client.play(webpage_url)。"""
    cog = _make_cog()
    song = _song()
    cog.stream_queue = [song]
    cog.stream_mode = True
    cog._prefetch_cache[song["url"]] = _done_future(None)

    fake_client = MagicMock()
    fake_client.play = AsyncMock(return_value=True)

    with patch("cogs.music_cog._get_puck_client", return_value=fake_client):
        await cog._stream_loop()
        await asyncio.sleep(0)   # 讓 create_task 起的 _fire_puck_play 真的跑

    fake_client.play.assert_awaited_once_with(
        song["webpage_url"], title=song["title"], seek=None, duration=None)


@pytest.mark.asyncio
async def test_stream_loop_passes_highlight_start_s_as_seek_to_puck_play():
    """YouTube 熱力圖精華起點（highlight_start_s）當 seek 傳給裝置端 client——ESP32
    的 client 目前忽略這個欄位（見 _fire_puck_play docstring），接受它只是維持跟
    其他 client 同款介面。"""
    cog = _make_cog()
    song = _song()
    song["highlight_start_s"] = 12.3
    song["duration"] = 200.0
    cog.stream_queue = [song]
    cog.stream_mode = True
    cog._prefetch_cache[song["url"]] = _done_future(None)

    fake_client = MagicMock()
    fake_client.play = AsyncMock(return_value=True)

    with patch("cogs.music_cog._get_puck_client", return_value=fake_client):
        await cog._stream_loop()
        await asyncio.sleep(0)

    fake_client.play.assert_awaited_once_with(
        song["webpage_url"], title=song["title"], seek=12.3, duration=200.0)


@pytest.mark.asyncio
async def test_stream_loop_skips_puck_play_when_already_played_in_tail(monkeypatch):
    """已經被 _fire_puck_crossfade 接手過的歌（_dj_played_in_tail=True）→ 不重複 play。
    明確 delenv 避免這台機器 .env 的 MARVIN_CAR_HARDWARE=pi_bt（main_discord.py
    import 時 load_dotenv() 會把它留在 os.environ 一整個 pytest session）滲進來
    把這條測成 pi_bt 路徑（pi_bt 底下 _get_puck_client() 永遠回 None，跟這裡想測的
    esp32_edge_mix dedup 邏輯是兩回事）。"""
    monkeypatch.delenv("MARVIN_CAR_HARDWARE", raising=False)
    cog = _make_cog()
    song = _song(played_in_tail=True)
    cog.stream_queue = [song]
    cog.stream_mode = True
    cog._prefetch_cache[song["url"]] = _done_future(None)

    fake_client = MagicMock()
    fake_client.play = AsyncMock(return_value=True)

    with patch("cogs.music_cog._get_puck_client", return_value=fake_client):
        await cog._stream_loop()
        await asyncio.sleep(0)

    fake_client.play.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_loop_skips_puck_play_when_no_puck_client():
    """非 esp32_edge_mix 硬體（_get_puck_client 回 None，例如 pi_bt 車 puck、家用
    Pi 3B）→ 零行為改變，不噴例外。pi_bt 現在完全走這條路：換歌/DJ口白改由
    /audio_stream 共用 mixer 處理，不再需要任何 puck_client 硬 play。"""
    cog = _make_cog()
    song = _song()
    cog.stream_queue = [song]
    cog.stream_mode = True
    cog._prefetch_cache[song["url"]] = _done_future(None)

    with patch("cogs.music_cog._get_puck_client", return_value=None):
        await cog._stream_loop()
        await asyncio.sleep(0)
    # 沒 client 就不該噴例外——跑到這裡沒 raise 就是過。
