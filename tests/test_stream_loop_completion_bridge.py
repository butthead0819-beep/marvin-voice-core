"""Characterization test（拆解 _stream_loop 前先鎖住，見 2026-09-11 /plan-eng-review）：
bridge `music_ended` 事件的 `completion` 值在三種路徑下的斷法。

既有 7 個 _stream_loop 相關測試沒有一條斷言這個值（outside voice #8）。規則見
music_cog.py:2325-2328：

    completion = playback_completion if self.stream_mode else "stopped"

`playback_completion` 是 try 區塊內的局部變數（預設 "natural"，例外時設 "stopped"），
但即使它還是 "natural"，只要當下 `self.stream_mode` 已被外部改成 False（例如使用者
中途喊「停」），照樣要回報 "stopped"——不能只看 playback_completion 那個局部旗標。
拆解後 `_stream_loop_prepare_and_announce`/`_stream_loop_retry_if_dropped` 等新方法
都不碰這段，它明確留在主迴圈（見設計檔），這裡鎖住的是主迴圈本身的既有行為。
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
    bot.cogs.get.return_value = vc

    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
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
async def test_stream_loop_emits_natural_completion_on_normal_playback():
    cog, vc = _make_cog_with_vc()
    song = _song()
    cog.stream_queue = [song]
    cog.stream_mode = True
    cog._prefetch_cache[song["url"]] = _done_future(None)
    cog.play_stream_song = AsyncMock()  # 正常播完，不拋例外、不動 stream_mode

    with patch("cogs.music_cog._get_puck_client", return_value=None), \
         patch("bridge_emitters.emit_music_ended_to_bridge", new=AsyncMock()) as mock_emit:
        await cog._stream_loop()
        await asyncio.sleep(0)  # 讓 create_task 起的 emit 真的跑一次，避免 unawaited warning

    assert mock_emit.call_args.args[2] == "natural"


@pytest.mark.asyncio
async def test_stream_loop_emits_stopped_completion_when_stream_mode_flips_mid_play():
    """歌播放中途 stream_mode 被外部設 False（例如使用者喊「停」）——即使
    play_stream_song 本身沒拋例外（歌是「自然」播完的），finally 仍要回報 stopped，
    不能因為 playback_completion 局部變數還是 "natural" 就誤報。"""
    cog, vc = _make_cog_with_vc()
    song = _song()
    cog.stream_queue = [song]
    cog.stream_mode = True
    cog._prefetch_cache[song["url"]] = _done_future(None)

    async def _play_then_stop(*a, **kw):
        cog.stream_mode = False

    cog.play_stream_song = AsyncMock(side_effect=_play_then_stop)

    with patch("cogs.music_cog._get_puck_client", return_value=None), \
         patch("bridge_emitters.emit_music_ended_to_bridge", new=AsyncMock()) as mock_emit:
        await cog._stream_loop()
        await asyncio.sleep(0)

    assert mock_emit.call_args.args[2] == "stopped"


@pytest.mark.asyncio
async def test_stream_loop_emits_stopped_completion_on_playback_exception():
    """play_stream_song 拋例外 → playback_completion="stopped" + raise，外層
    except Exception 接住（stream_mode 收攤），不炸出 _stream_loop() 本身。"""
    cog, vc = _make_cog_with_vc()
    song = _song()
    cog.stream_queue = [song]
    cog.stream_mode = True
    cog._prefetch_cache[song["url"]] = _done_future(None)
    cog.play_stream_song = AsyncMock(side_effect=RuntimeError("ffmpeg boom"))

    with patch("cogs.music_cog._get_puck_client", return_value=None), \
         patch("bridge_emitters.emit_music_ended_to_bridge", new=AsyncMock()) as mock_emit:
        await cog._stream_loop()  # 不應把例外往外拋
        await asyncio.sleep(0)

    assert mock_emit.call_args.args[2] == "stopped"
