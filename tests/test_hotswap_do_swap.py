"""_do_hotswap 回歸測試：熱切換只能停「播放」，不能停「收音」。

discord-ext-voice-recv 同一個 VoiceClient 同時管 play 與 receive，`vc.stop()` 會一起
呼叫 stop_playing() + stop_listening()。曾有 bug：熱切換用 vc.stop() 把收音 reader 也殺了，
切換成功後 STT 整條死掉、再喚醒無回應。修正是改用 vc.stop_playing()。此測試鎖住這個回歸。
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from cogs.voice_controller import VoiceController


def make_controller():
    c = VoiceController.__new__(VoiceController)
    c._hotswap_coord = MagicMock()
    c._hotswap_coord.begin_swap.return_value = object()  # src2 sentinel
    c._stream_play_gen = 0
    c.playback_lock = asyncio.Lock()
    c._stream_position_source = None
    return c


@pytest.mark.asyncio
async def test_do_hotswap_stops_playing_not_listening():
    c = make_controller()
    vc = MagicMock()
    vc.is_playing.return_value = False  # 讓等待迴圈立即跳出

    await c._do_hotswap(vc, asyncio.get_running_loop(), asyncio.Event())

    vc.stop_playing.assert_called_once()
    vc.stop.assert_not_called()  # vc.stop() 會連 stop_listening() 一起殺掉收音
    vc.play.assert_called_once()
    c._hotswap_coord.finish_swap.assert_called_once()
