"""TDD：緊急停止中不准啟動任何音樂（串流/電台/個人歌單），只有 /marvin_estop 解除才恢復。

_estop_blocked 透過 self._vc()（VoiceController cog）讀 _estop_active()，見
cogs/music_cog_subsystem.py 與 cogs/voice_controller_estop.py。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _make_cog(estop_active: bool):
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    vc = MagicMock()
    vc._estop_active.return_value = estop_active
    bot.cogs.get.return_value = vc
    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    return cog, vc


async def _noop():
    return None


def _song(title="左邊的人-陳華 歌詞字幕版", who="狗與露"):
    return {"title": title, "requested_by": who, "url": "x",
            "webpage_url": "https://youtu.be/tER-0RhdAow"}


@pytest.mark.asyncio
async def test_ensure_stream_loop_blocked_during_estop():
    cog, _vc = _make_cog(estop_active=True)
    cog.stream_mode = False
    cog.stream_queue = [_song()]
    assert cog._ensure_stream_loop() is False
    assert cog.stream_task is None
    assert cog.stream_mode is False


@pytest.mark.asyncio
async def test_ensure_stream_loop_starts_normally_when_not_estop():
    cog, _vc = _make_cog(estop_active=False)
    cog._stream_loop = MagicMock(return_value=_noop())
    cog.stream_mode = False
    cog.stream_queue = [_song()]
    assert cog._ensure_stream_loop() is True
    assert cog.stream_task is not None
    cog.stream_task.cancel()


@pytest.mark.asyncio
async def test_start_radio_blocked_during_estop():
    cog, _vc = _make_cog(estop_active=True)
    await cog.start_radio("測試")
    assert cog.radio_mode is False


@pytest.mark.asyncio
async def test_start_personal_shuffle_blocked_during_estop():
    cog, _vc = _make_cog(estop_active=True)
    ok, msg = await cog.start_personal_shuffle("狗與露")
    assert ok is False
    assert "緊急停止" in msg
    assert cog.stream_task is None


def test_estop_blocked_false_when_vc_missing():
    bot = MagicMock()
    bot.cogs.get.return_value = None
    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    assert cog._estop_blocked("串流") is False
