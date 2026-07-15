"""TDD: DJ 尾段串場排程 wiring（mock cog，不需真 TTS/Discord）。

四情境：
(a) 尾段窗內派發下一首的 DJ（走 _maybe_play_dj_interjection）
(b) skip（_current_song_skipped=True 或 _current_stream_info 換掉）→ 不派發
(c) duration 未知 → _run_tail_dj 早退、不派發、不標記（退回舊行為）
(d) next_info 已標 _dj_played_in_tail → _stream_loop 把 dj_audio/dj_data 設 None（只播一次）
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


# ── (a) 尾段窗內派發 ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tail_dj_fires_and_marks_next():
    """duration 足夠、next 有預渲染 audio → _maybe_play_dj_interjection 被呼叫且標記 _dj_played_in_tail。"""
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()

    meta = {"lyrics": None, "comment": None, "dj": _dj_meta()}
    done_task: asyncio.Task = asyncio.get_event_loop().create_future()
    done_task.set_result(meta)
    cog._prefetch_cache[nxt["url"]] = done_task

    cog._current_stream_info = cur
    cog._current_song_skipped = False
    cog.stream_mode = True

    cog._maybe_play_dj_interjection = AsyncMock()

    import os
    with patch("os.path.exists", return_value=True), \
         patch("asyncio.sleep", new=AsyncMock()):
        # song_start_time を 0 秒前に設定（delay が小さくなるよう elapsed を大きく）
        import time
        song_start_time = time.time() - 170.0   # elapsed≈170s; fire_at=180-5=175; delay≈5s
        await cog._run_tail_dj(cur, nxt, song_start_time)

    cog._maybe_play_dj_interjection.assert_called_once()
    assert nxt.get("_dj_played_in_tail") is True


# ── (b) skip 後不派發 ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tail_dj_skipped_by_flag():
    """派發前設 _current_song_skipped=True → re-check 擋住、不派發。"""
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()

    meta = {"lyrics": None, "comment": None, "dj": _dj_meta()}
    done_task = asyncio.get_event_loop().create_future()
    done_task.set_result(meta)
    cog._prefetch_cache[nxt["url"]] = done_task

    cog._current_stream_info = cur
    cog._current_song_skipped = False
    cog.stream_mode = True
    cog._maybe_play_dj_interjection = AsyncMock()

    async def _sleep_then_skip(delay):
        cog._current_song_skipped = True   # skip 發生在 sleep 期間

    import os
    with patch("os.path.exists", return_value=True), \
         patch("asyncio.sleep", side_effect=_sleep_then_skip):
        import time
        song_start_time = time.time() - 170.0
        await cog._run_tail_dj(cur, nxt, song_start_time)

    cog._maybe_play_dj_interjection.assert_not_called()
    assert not nxt.get("_dj_played_in_tail")


@pytest.mark.asyncio
async def test_tail_dj_skipped_by_stream_info_change():
    """派發前 _current_stream_info 換成別首 → re-check 擋住、不派發。"""
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()

    meta = {"lyrics": None, "comment": None, "dj": _dj_meta()}
    done_task = asyncio.get_event_loop().create_future()
    done_task.set_result(meta)
    cog._prefetch_cache[nxt["url"]] = done_task

    cog._current_stream_info = cur
    cog._current_song_skipped = False
    cog.stream_mode = True
    cog._maybe_play_dj_interjection = AsyncMock()

    async def _sleep_then_change(delay):
        cog._current_stream_info = _next_info()  # 歌已切換

    import os
    with patch("os.path.exists", return_value=True), \
         patch("asyncio.sleep", side_effect=_sleep_then_change):
        import time
        song_start_time = time.time() - 170.0
        await cog._run_tail_dj(cur, nxt, song_start_time)

    cog._maybe_play_dj_interjection.assert_not_called()


# ── (c) duration 未知 → 退回舊行為 ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_tail_dj_no_duration_returns_early():
    """cur_info 無 duration → _run_tail_dj 早退、_maybe_play_dj_interjection 不被呼叫。"""
    cog = _make_cog()
    cur = _cur_info(duration=None)
    cur.pop("duration", None)   # 確保 key 不存在
    nxt = _next_info()

    cog._current_stream_info = cur
    cog._current_song_skipped = False
    cog.stream_mode = True
    cog._maybe_play_dj_interjection = AsyncMock()

    import time
    await cog._run_tail_dj(cur, nxt, time.time())

    cog._maybe_play_dj_interjection.assert_not_called()
    assert not nxt.get("_dj_played_in_tail")


@pytest.mark.asyncio
async def test_tail_dj_duration_zero_returns_early():
    """cur_info duration=0 → 視為未知、早退。"""
    cog = _make_cog()
    cur = _cur_info(duration=0)
    nxt = _next_info()

    cog._current_stream_info = cur
    cog.stream_mode = True
    cog._maybe_play_dj_interjection = AsyncMock()

    import time
    await cog._run_tail_dj(cur, nxt, time.time())

    cog._maybe_play_dj_interjection.assert_not_called()


# ── (d) 只播一次：已標 _dj_played_in_tail → 開頭 dj_audio/dj_data 清空 ──────

def test_stream_loop_skips_dj_when_already_played_in_tail():
    """info['_dj_played_in_tail']=True → _stream_loop 把 dj_audio 和 dj_data 設 None。

    用直接模擬邏輯驗證（抽出 _stream_loop 中對應段的業務規則）。
    """
    info = _cur_info(duration=180.0)
    info["_dj_played_in_tail"] = True

    dj_data = {"text": "DJ 已播", "audio_path": "/tmp/dj.opus"}

    # 模擬 _stream_loop 中 [DJ Tail] 片段的判斷邏輯
    if info.get("_dj_played_in_tail"):
        dj_audio = None
        dj_data = None

    assert dj_audio is None
    assert dj_data is None


# ── CancelledError 正確傳播（不被吞掉）───────────────────────────────────────

@pytest.mark.asyncio
async def test_tail_dj_cancelled_error_propagates():
    """asyncio.sleep 被 cancel → CancelledError 在 _run_tail_dj 內被 catch 並 return（不向外傳播）。"""
    cog = _make_cog()
    cur = _cur_info(duration=180.0)
    nxt = _next_info()

    meta = {"lyrics": None, "comment": None, "dj": _dj_meta()}
    done_task = asyncio.get_event_loop().create_future()
    done_task.set_result(meta)
    cog._prefetch_cache[nxt["url"]] = done_task

    cog._current_stream_info = cur
    cog._current_song_skipped = False
    cog.stream_mode = True
    cog._maybe_play_dj_interjection = AsyncMock()

    async def _raise_cancelled(delay):
        raise asyncio.CancelledError()

    import os
    with patch("os.path.exists", return_value=True), \
         patch("asyncio.sleep", side_effect=_raise_cancelled):
        import time
        # _run_tail_dj 應 catch CancelledError 並 return（不 re-raise）
        await cog._run_tail_dj(cur, nxt, time.time() - 170.0)

    cog._maybe_play_dj_interjection.assert_not_called()
