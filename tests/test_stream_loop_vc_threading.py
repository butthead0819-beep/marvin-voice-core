"""Characterization test（拆解 _stream_loop 前先鎖住，見 2026-09-11 /plan-eng-review）：
vc **非 None** 時 _stream_loop 正常跑完一輪。

唯二會整條跑 _stream_loop() 的既有測試（test_stream_loop_revive.py /
test_puck_play_first_song.py）目前都把 `bot.cogs.get.return_value` 設成 None，
抓不到「_stream_loop_prepare_and_announce / _stream_loop_schedule_tail_dj 忘記把
vc 當參數傳、內部改用 self._vc() 重查」這類要等到拆解後才會現形的斷法（outside
voice #1）。這裡補上 vc 非 None 的路徑，鎖住：
  - `_current_stream_start_time` 真的被設值（line 2287，HUD 進度條依賴）
  - `_republish_queue_snapshot` 在 meta 就緒那條路徑被呼叫（line 2192）
  - autopilot 補位真的用了這個 vc（`vc.get_online_members()` 被呼叫），不是靜默
    落到 vc-is-None 的短路分支
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_cog_with_vc():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.music_memory = MagicMock()
    bot.music_memory._key = MagicMock(return_value="key")
    bot.music_memory._data = {"songs": {}}
    bot.music_memory.time_slot = MagicMock(return_value="深夜")

    vc = MagicMock()
    vc.active_text_channel = None
    vc.voice_client = None
    vc.get_online_members = MagicMock(return_value=[])
    vc.last_marvin_speech_time = 0
    vc.stt_logger = MagicMock()
    bot.cogs.get.return_value = vc  # _vc() → 這個 vc（非 None，是這批測試的重點）

    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    cog.play_stream_song = AsyncMock()
    # 播完一首後佇列空 → 讓迴圈乾淨結束，不要觸發真的 autopilot/yt-dlp 呼叫
    # （比照 test_puck_play_first_song.py 的 _make_cog 慣例）。
    cog._auto_recommend = AsyncMock()
    cog._last_resort_replay = AsyncMock(return_value=False)
    return cog, vc


def _done_future(value):
    fut = asyncio.get_event_loop().create_future()
    fut.set_result(value)
    return fut


def _song():
    return {"title": "測試歌", "url": "https://ex/resolved-cdn-url",
            "webpage_url": "https://youtube.com/watch?v=abc123", "requested_by": "狗與露"}


@pytest.mark.asyncio
async def test_stream_loop_completes_with_real_vc_and_sets_start_time():
    cog, vc = _make_cog_with_vc()
    song = _song()
    cog.stream_queue = [song]
    cog.stream_mode = True
    cog._prefetch_cache[song["url"]] = _done_future(None)

    with patch("cogs.music_cog._get_puck_client", return_value=None):
        await cog._stream_loop()

    assert cog._current_stream_start_time is not None
    # autopilot 補位路徑真的走了這個 vc，不是因為 vc 被誤判 None 而短路跳過。
    vc.get_online_members.assert_called()


@pytest.mark.asyncio
async def test_stream_loop_republishes_queue_snapshot_after_meta_resolved():
    cog, vc = _make_cog_with_vc()
    song = _song()
    cog.stream_queue = [song]
    cog.stream_mode = True
    # meta 已就緒（非 None dict）→ 命中 line 2192 的 _republish_queue_snapshot() 呼叫。
    cog._prefetch_cache[song["url"]] = _done_future({"comment": None, "lyrics": None, "dj": None})
    cog._republish_queue_snapshot = MagicMock(wraps=cog._republish_queue_snapshot)

    with patch("cogs.music_cog._get_puck_client", return_value=None):
        await cog._stream_loop()

    # meta 就緒那次（2192）+ song_start_time 設定那次（2288，此路徑必經）。
    assert cog._republish_queue_snapshot.call_count >= 2
