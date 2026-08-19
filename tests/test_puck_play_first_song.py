"""TDD: esp32_edge_mix 硬體上，開場第一首（沒經過 _fire_puck_crossfade 接手）要送
puck_client.play(webpage_url) 讓 ESP32 從乾淨狀態開播——STEP 11 rollback commit 點出的
缺口：production 只有 _run_tail_dj 會叫 queue_next+crossfade，開場第一首/skip 之後那首
從沒人叫過 play，兩個 deck 永遠閒置沒聲音。見 cogs/music_cog.py::_stream_loop 裡
`if not _dj_played_in_tail:` 那段、_fire_puck_play()。
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
    fake_client.status = AsyncMock(return_value=None)  # Pi 端沒在播這首，不該跳過補 play

    with patch("cogs.music_cog._get_puck_client", return_value=fake_client):
        await cog._stream_loop()
        await asyncio.sleep(0)   # 讓 create_task 起的 _fire_puck_play 真的跑

    fake_client.play.assert_awaited_once_with(
        song["webpage_url"], title=song["title"], seek=None)


@pytest.mark.asyncio
async def test_stream_loop_passes_highlight_start_s_as_seek_to_puck_play():
    """2026-08-19：YouTube 熱力圖精華起點（highlight_start_s）該當 seek 傳給
    Pi，讓 Pi 也跳過前奏，跟 Discord 本地播放內容對齊（見
    project_car_puck_mk2... 記憶「提前結束約10秒」根因）。"""
    cog = _make_cog()
    song = _song()
    song["highlight_start_s"] = 12.3
    cog.stream_queue = [song]
    cog.stream_mode = True
    cog._prefetch_cache[song["url"]] = _done_future(None)

    fake_client = MagicMock()
    fake_client.play = AsyncMock(return_value=True)
    fake_client.status = AsyncMock(return_value=None)

    with patch("cogs.music_cog._get_puck_client", return_value=fake_client):
        await cog._stream_loop()
        await asyncio.sleep(0)

    fake_client.play.assert_awaited_once_with(
        song["webpage_url"], title=song["title"], seek=12.3)


@pytest.mark.asyncio
async def test_stream_loop_skips_puck_play_when_already_played_in_tail(monkeypatch):
    """已經被 _fire_puck_crossfade 接手過的歌（_dj_played_in_tail=True）→ 不重複 play。
    這條測的是 esp32_edge_mix/其餘硬體路徑（pi_bt 有自己獨立的
    _puck_pi_bt_handed_off 判斷，見下面兩條 pi_bt 測試）——明確 delenv 避免這台
    機器 .env 的 MARVIN_CAR_HARDWARE=pi_bt（main_discord.py import 時 load_dotenv()
    會把它留在 os.environ 一整個 pytest session）滲進來把這條測成 pi_bt 路徑。"""
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
async def test_stream_loop_fires_puck_play_on_pi_bt_when_dj_tail_played_but_puck_not_handed_off(monkeypatch):
    """2026-08-19 實機踩到：pi_bt 硬體上 _dj_played_in_tail=True（DJ 口白有講話）
    不代表 Pi 端真的接到 crossfade（例如 Pi 剛好離線、queue_next 失敗）——沿用
    _dj_played_in_tail 判斷會讓這裡永久跳過硬 play，Pi 端 deck_a 停在 None，之後
    crossfade 邏輯也不會 promote 空的 deck_a，變成永久靜音。pi_bt 該看
    _puck_pi_bt_handed_off（_run_puck_pi_bt_crossfade 依裝置端實際成功與否設的
    旗標），沒接手成功就該補一次硬 play。"""
    monkeypatch.setenv("MARVIN_CAR_HARDWARE", "pi_bt")
    cog = _make_cog()
    song = _song(played_in_tail=True)   # DJ 口白講了話，但裝置端沒接手
    song["_puck_pi_bt_handed_off"] = False
    cog.stream_queue = [song]
    cog.stream_mode = True
    cog._prefetch_cache[song["url"]] = _done_future(None)

    fake_client = MagicMock()
    fake_client.play = AsyncMock(return_value=True)
    fake_client.status = AsyncMock(return_value=None)

    with patch("cogs.music_cog._get_puck_client", return_value=fake_client):
        await cog._stream_loop()
        await asyncio.sleep(0)

    fake_client.play.assert_awaited_once_with(song["webpage_url"], title=song["title"], seek=None)


@pytest.mark.asyncio
async def test_stream_loop_skips_puck_play_on_pi_bt_when_handed_off(monkeypatch):
    """pi_bt 硬體上，_puck_pi_bt_handed_off=True（裝置端真的接到 crossfade）→
    不重複 play，即使 _dj_played_in_tail 是 False 也一樣（兩個旗標互不影響）。"""
    monkeypatch.setenv("MARVIN_CAR_HARDWARE", "pi_bt")
    cog = _make_cog()
    song = _song()   # 沒有 _dj_played_in_tail
    song["_puck_pi_bt_handed_off"] = True
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
async def test_stream_loop_skips_hard_play_when_pi_already_self_healed(monkeypatch):
    """2026-08-19 實機踩到：Mac 記錄的是「FIRE 時輪詢逾時、crossfade 失敗」
    （_puck_pi_bt_handed_off=False），但 Pi 端可能在那之後、deck_b 真正 ready
    時自己已經扶正了（見 device/puck_mixer.py::_loop() 的 eof_event 自我修復）。
    兩個獨立保險互不知情撞在一起，會變成「Pi 已經自己救活、Mac 又送一次硬
    play 砍掉重開」——聽感是歌曲順利接上、播了幾秒後又從頭開始。硬 play 前
    該先問一次 Pi 現在是不是已經在播這首，對得上就跳過。"""
    monkeypatch.setenv("MARVIN_CAR_HARDWARE", "pi_bt")
    cog = _make_cog()
    song = _song(played_in_tail=True)
    song["_puck_pi_bt_handed_off"] = False   # Mac 記錄的是失敗
    cog.stream_queue = [song]
    cog.stream_mode = True
    cog._prefetch_cache[song["url"]] = _done_future(None)

    fake_client = MagicMock()
    fake_client.play = AsyncMock(return_value=True)
    fake_client.status = AsyncMock(return_value={"playing": song["webpage_url"]})  # Pi 已經自救成功

    with patch("cogs.music_cog._get_puck_client", return_value=fake_client):
        await cog._stream_loop()
        await asyncio.sleep(0)

    fake_client.play.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_loop_skips_puck_play_when_no_puck_client():
    """非 esp32_edge_mix 硬體（_get_puck_client 回 None，例如家用 Pi 3B）→ 零行為改變。"""
    cog = _make_cog()
    song = _song()
    cog.stream_queue = [song]
    cog.stream_mode = True
    cog._prefetch_cache[song["url"]] = _done_future(None)

    with patch("cogs.music_cog._get_puck_client", return_value=None):
        await cog._stream_loop()
        await asyncio.sleep(0)
    # 沒 client 就不該噴例外——跑到這裡沒 raise 就是過。
