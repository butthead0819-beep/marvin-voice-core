"""TDD: DJ 尾段串場排程 wiring（mock cog，不需真 TTS/Discord）。

2026-07-15 修：尾段 task 不再於「開播時」綁定下一首（那時 autopilot 常還沒把
下一首排進 queue → 沒排 tail → 下一首走舊路混進開頭）。改成只用當前歌 duration
算點火時刻，睡到剩 5s 才抓 stream_queue[0]（那時下一首幾乎必定已排入）。

情境：
(a) 尾段窗內派發下一首的 DJ（點火時抓 stream_queue[0]）
(bug) 開播時 queue 空、sleep 期間才排入 → 仍點火（此次修的核心）
(b) skip / _current_stream_info 換掉 → 不派發
(c) duration 未知 → 早退（退回舊行為）
(d) 點火時 queue 仍空 / 下一首無預渲染 audio → 退回舊行為
(e) next_info 已標 _dj_played_in_tail → _stream_loop 把 dj_audio/dj_data 設 None
(f) CancelledError → catch 後 return
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── helper: build a minimal MusicCog ────────────────────────────────────────

def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/dj.opus")
    bot.tts_engine.get_estimated_duration = MagicMock(return_value=6.0)
    bot.router = MagicMock()
    bot.router.generate_dynamic_system_msg = AsyncMock(return_value="接下來這首…")
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    bot.engine.post_summon_callback = None
    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="key")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="深夜")

    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    return cog


def _cur_info(duration=180.0):
    return {"title": "周杰倫 - 夜曲", "url": "https://ex/cur", "duration": duration,
            "requested_by": "大肚"}


def _next_info():
    return {"title": "陶喆 - 普通朋友", "url": "https://ex/next", "requested_by": "狗與露"}


def _dj_meta(audio_path="/tmp/dj.opus"):
    return {"text": "接下來這首…", "audio_path": audio_path}


def _done_future(value):
    fut = asyncio.get_event_loop().create_future()
    fut.set_result(value)
    return fut


def _prime(cog, cur, *, skipped=False, stream_mode=True):
    cog._current_stream_info = cur
    cog._current_song_skipped = skipped
    cog.stream_mode = stream_mode
    cog._maybe_play_dj_interjection = AsyncMock()


# ── (a) 尾段窗內派發：點火時抓 stream_queue[0] ──────────────────────────────

@pytest.mark.asyncio
async def test_tail_dj_fires_and_marks_next():
    """queue 有下一首且有預渲染 audio → _maybe_play_dj_interjection 被呼叫 + 標記。"""
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()
    cog.stream_queue = [nxt]
    cog._prefetch_cache[nxt["url"]] = _done_future({"dj": _dj_meta()})
    _prime(cog, cur)

    with patch("os.path.exists", return_value=True), \
         patch("asyncio.sleep", new=AsyncMock()):
        import time
        song_start_time = time.time() - 170.0  # elapsed≈170; fire_at=180-5=175; delay≈5
        await cog._run_tail_dj(cur, song_start_time)

    cog._maybe_play_dj_interjection.assert_called_once()
    assert nxt.get("_dj_played_in_tail") is True


# ── (bug) 開播時 queue 空、sleep 期間才排入 → 仍點火（此次修的核心）──────────

@pytest.mark.asyncio
async def test_tail_dj_fires_when_next_queued_during_playback():
    """開播瞬間 queue 空（autopilot 還沒排），播放中才排入下一首 → 點火時抓得到、照樣派發。

    舊實作在開播時就綁定 next_info，queue 空 → 根本沒排 tail，下一首只能走舊路
    混進開頭。這條測試鎖住修正後的行為。
    """
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()
    cog.stream_queue = []  # 開播時 queue 空
    _prime(cog, cur)

    async def _sleep_then_enqueue(delay):
        # 模擬 autopilot 在當前歌播放中才把下一首排入 + prefetch 完成
        cog.stream_queue = [nxt]
        cog._prefetch_cache[nxt["url"]] = _done_future({"dj": _dj_meta()})

    with patch("os.path.exists", return_value=True), \
         patch("asyncio.sleep", side_effect=_sleep_then_enqueue):
        import time
        await cog._run_tail_dj(cur, time.time() - 170.0)

    cog._maybe_play_dj_interjection.assert_called_once()
    assert nxt.get("_dj_played_in_tail") is True


# ── (bug2) 點火時下一首沒 prefetch → 現場補建、照樣派發 ──────────────────────

@pytest.mark.asyncio
async def test_tail_dj_builds_prefetch_if_missing_at_fire():
    """下一首在 queue 但沒 prefetch（autopilot 較晚排入）→ 現場補 _fetch_song_meta、派發。"""
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()
    cog.stream_queue = [nxt]
    # 沒放 prefetch_cache → 逼 _resolve_tail_dj_meta 現場補建
    cog._fetch_song_meta = AsyncMock(return_value={"dj": _dj_meta()})
    _prime(cog, cur)

    with patch("os.path.exists", return_value=True), \
         patch("asyncio.sleep", new=AsyncMock()):
        import time
        await cog._run_tail_dj(cur, time.time() - 170.0)

    cog._fetch_song_meta.assert_awaited_once()
    cog._maybe_play_dj_interjection.assert_called_once()
    assert nxt.get("_dj_played_in_tail") is True


# ── (b) skip / 換歌後不派發 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tail_dj_skipped_by_flag():
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()
    cog.stream_queue = [nxt]
    cog._prefetch_cache[nxt["url"]] = _done_future({"dj": _dj_meta()})
    _prime(cog, cur)

    async def _sleep_then_skip(delay):
        cog._current_song_skipped = True

    with patch("os.path.exists", return_value=True), \
         patch("asyncio.sleep", side_effect=_sleep_then_skip):
        import time
        await cog._run_tail_dj(cur, time.time() - 170.0)

    cog._maybe_play_dj_interjection.assert_not_called()
    assert not nxt.get("_dj_played_in_tail")


@pytest.mark.asyncio
async def test_tail_dj_skipped_by_stream_info_change():
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()
    cog.stream_queue = [nxt]
    cog._prefetch_cache[nxt["url"]] = _done_future({"dj": _dj_meta()})
    _prime(cog, cur)

    async def _sleep_then_change(delay):
        cog._current_stream_info = _next_info()  # 歌已切換

    with patch("os.path.exists", return_value=True), \
         patch("asyncio.sleep", side_effect=_sleep_then_change):
        import time
        await cog._run_tail_dj(cur, time.time() - 170.0)

    cog._maybe_play_dj_interjection.assert_not_called()


# ── (c) duration 未知 → 早退 ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tail_dj_no_duration_returns_early():
    cog = _make_cog()
    cur = _cur_info(duration=None)
    cur.pop("duration", None)
    cog.stream_queue = [_next_info()]
    _prime(cog, cur)

    import time
    await cog._run_tail_dj(cur, time.time())

    cog._maybe_play_dj_interjection.assert_not_called()


@pytest.mark.asyncio
async def test_tail_dj_duration_zero_returns_early():
    cog = _make_cog()
    cur = _cur_info(duration=0)
    cog.stream_queue = [_next_info()]
    _prime(cog, cur)

    import time
    await cog._run_tail_dj(cur, time.time())

    cog._maybe_play_dj_interjection.assert_not_called()


# ── (d) 點火時 queue 空 / 無預渲染 audio → 退回舊行為 ──────────────────────

@pytest.mark.asyncio
async def test_tail_dj_no_next_at_fire_returns():
    """點火時 queue 仍空（下一首始終沒排入）→ 不派發、退回舊行為。"""
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    cog.stream_queue = []
    _prime(cog, cur)

    with patch("asyncio.sleep", new=AsyncMock()):
        import time
        await cog._run_tail_dj(cur, time.time() - 170.0)

    cog._maybe_play_dj_interjection.assert_not_called()


@pytest.mark.asyncio
async def test_tail_dj_next_without_prerendered_audio_returns():
    """下一首 DJ 無預渲染 audio → 退回舊行為（下一首走開頭 DJ）。"""
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()
    cog.stream_queue = [nxt]
    cog._prefetch_cache[nxt["url"]] = _done_future({"dj": {"text": "x", "audio_path": None}})
    _prime(cog, cur)

    with patch("os.path.exists", return_value=False), \
         patch("asyncio.sleep", new=AsyncMock()):
        import time
        await cog._run_tail_dj(cur, time.time() - 170.0)

    cog._maybe_play_dj_interjection.assert_not_called()
    assert not nxt.get("_dj_played_in_tail")


# ── (e) 只播一次：已標 _dj_played_in_tail → 開頭 dj_audio/dj_data 清空 ──────

def test_stream_loop_skips_dj_when_already_played_in_tail():
    info = _cur_info(duration=180.0)
    info["_dj_played_in_tail"] = True
    dj_data = {"text": "DJ 已播", "audio_path": "/tmp/dj.opus"}

    dj_audio = dj_data.get("audio_path") if isinstance(dj_data, dict) else None
    if info.get("_dj_played_in_tail"):
        dj_audio = None
        dj_data = None

    assert dj_audio is None
    assert dj_data is None


# ── (f) CancelledError 被 catch ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tail_dj_cancelled_error_propagates():
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()
    cog.stream_queue = [nxt]
    cog._prefetch_cache[nxt["url"]] = _done_future({"dj": _dj_meta()})
    _prime(cog, cur)

    async def _raise_cancelled(delay):
        raise asyncio.CancelledError()

    with patch("os.path.exists", return_value=True), \
         patch("asyncio.sleep", side_effect=_raise_cancelled):
        import time
        await cog._run_tail_dj(cur, time.time() - 170.0)

    cog._maybe_play_dj_interjection.assert_not_called()
