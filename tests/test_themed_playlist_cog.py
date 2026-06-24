"""主題歌單 Step 3b：MusicCog 觸發 gate（env + 冷卻 + 每晚上限 + 跨日重置）。

實際入隊/LLM/resolve 由 themed_playlist 模組測試 + 離線 dryrun 覆蓋；這裡只鎖 gate 邏輯，
因為它決定「會不會打付費 LLM、會不會在播放核心啟動主題歌單」。
"""
from __future__ import annotations

import datetime
from unittest.mock import MagicMock

import pytest


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.music_memory = None
    from cogs.music_cog import MusicCog
    return MusicCog(bot)


_NOW = 10_000_000.0  # 任意未來時戳


def test_themed_gate_closed_when_env_off(monkeypatch):
    monkeypatch.delenv("MARVIN_THEMED_PLAYLIST", raising=False)
    assert _make_cog()._themed_gate_open(_NOW) is False


def test_themed_gate_open_when_env_on_fresh(monkeypatch):
    monkeypatch.setenv("MARVIN_THEMED_PLAYLIST", "1")
    cog = _make_cog()
    cog._last_themed_set_ts = 0.0
    cog._themed_sets_tonight = 0
    assert cog._themed_gate_open(_NOW) is True


def test_themed_gate_closed_during_cooldown(monkeypatch):
    monkeypatch.setenv("MARVIN_THEMED_PLAYLIST", "1")
    cog = _make_cog()
    cog._themed_set_date = datetime.date.fromtimestamp(_NOW)
    cog._last_themed_set_ts = _NOW - 60  # 1 分鐘前 < 冷卻
    cog._themed_sets_tonight = 0
    assert cog._themed_gate_open(_NOW) is False


def test_themed_gate_closed_when_nightly_cap_hit(monkeypatch):
    monkeypatch.setenv("MARVIN_THEMED_PLAYLIST", "1")
    cog = _make_cog()
    cog._themed_set_date = datetime.date.fromtimestamp(_NOW)
    cog._last_themed_set_ts = 0.0
    cog._themed_sets_tonight = cog._THEMED_SET_NIGHTLY_CAP
    assert cog._themed_gate_open(_NOW) is False


def test_themed_gate_resets_count_on_new_day(monkeypatch):
    monkeypatch.setenv("MARVIN_THEMED_PLAYLIST", "1")
    cog = _make_cog()
    cog._themed_set_date = datetime.date(2020, 1, 1)  # 舊日期
    cog._themed_sets_tonight = cog._THEMED_SET_NIGHTLY_CAP
    cog._last_themed_set_ts = 0.0
    assert cog._themed_gate_open(_NOW) is True          # 新的一天 → 重置 → 開
    assert cog._themed_sets_tonight == 0


@pytest.mark.asyncio
async def test_try_themed_set_noop_when_env_off(monkeypatch):
    """env off → _try_themed_set 直接回 0、不打 LLM、不碰佇列。"""
    monkeypatch.delenv("MARVIN_THEMED_PLAYLIST", raising=False)
    cog = _make_cog()
    cog.stream_queue = []
    n = await cog._try_themed_set(["狗與露"], [], "狗與露", MagicMock())
    assert n == 0
    assert cog.stream_queue == []
