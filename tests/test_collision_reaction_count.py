"""口白播出後 60 秒反應計數：aired 事件排程、寫 reaction 紀錄、例外不外拋。"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import dj_narration_log


def _make_cog(history):
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_history = MagicMock(return_value=history)
    bot.music_memory = MagicMock()
    from cogs.music_cog import MusicCog
    return MusicCog(bot)


@pytest.mark.asyncio
async def test_attach_narration_schedules_reaction_count(monkeypatch):
    monkeypatch.setattr(dj_narration_log, "log_dj_narration", lambda r: None)
    monkeypatch.setattr(dj_narration_log, "log_song_play", lambda r: None)
    cog = _make_cog([])
    cog._log_reaction_count = AsyncMock()
    cog._attach_narration({}, {"narration_id": "n1", "mode": "collision"}, "full")
    await asyncio.sleep(0)
    cog._log_reaction_count.assert_awaited_once()
    assert cog._log_reaction_count.await_args.args[0] == "n1"


@pytest.mark.asyncio
async def test_log_reaction_count_writes_pre_post(monkeypatch):
    records = []
    monkeypatch.setattr(dj_narration_log, "log_dj_narration", records.append)
    t0 = 1000.0
    history = [
        {"timestamp": 970.0, "speaker": "a", "text": "前"},
        {"timestamp": 1010.0, "speaker": "a", "text": "後"},
        {"timestamp": 1020.0, "speaker": "b", "text": "後"},
        {"timestamp": 1030.0, "speaker": "Marvin", "text": "口白"},
    ]
    cog = _make_cog(history)
    await cog._log_reaction_count("n1", t0, delay_s=0)
    assert records == [{"type": "reaction", "narration_id": "n1", "pre_60": 1, "post_60": 2}]


@pytest.mark.asyncio
async def test_log_reaction_count_swallows_errors(monkeypatch):
    monkeypatch.setattr(dj_narration_log, "log_dj_narration", lambda r: None)
    cog = _make_cog([])
    cog.bot.engine.conv_buffer.get_history = MagicMock(side_effect=RuntimeError("boom"))
    await cog._log_reaction_count("n1", 1000.0, delay_s=0)
