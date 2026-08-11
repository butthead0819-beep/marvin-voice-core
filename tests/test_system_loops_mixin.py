"""
SystemLoopsMixin — VoiceController 的週期性系統維護迴圈抽到獨立檔（減肥），
以 mixin 併入，self 身分不變、零行為改動。三個迴圈皆 @tasks.loop，由 cog_load
的 self.X.start() 經 MRO 正常啟動。
"""
from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock

import pytest
from discord.ext import tasks

MOD = "cogs.voice_controller_system_loops"
LOOPS = ["slow_system_loop", "daily_log_export_loop", "reset_stt_counter_loop"]


def test_mixin_in_mro():
    from cogs.voice_controller import VoiceController
    from cogs.voice_controller_system_loops import SystemLoopsMixin
    assert SystemLoopsMixin in VoiceController.__mro__


@pytest.mark.parametrize("name", LOOPS)
def test_loop_moved_and_is_loop(name):
    from cogs.voice_controller import VoiceController
    loop = getattr(VoiceController, name)
    assert isinstance(loop, tasks.Loop), f"{name} 不是 tasks.Loop"
    assert loop.coro.__module__ == MOD


def test_player_spoke_recently():
    """TTS duck 連續刷新閘：最近 window 秒內有 per-packet 發聲 → True（0=重置/無）。"""
    from cogs.voice_controller_system_loops import _player_spoke_recently
    now = 1000.0
    assert _player_spoke_recently(999.5, now) is True            # 0.5s 前 → 最近有講
    assert _player_spoke_recently(998.0, now) is False           # 2s 前 → 超過 1.5s 窗
    assert _player_spoke_recently(0.0, now) is False             # 已重置 / 無發聲
    assert _player_spoke_recently(999.0, now, window=0.5) is False  # 1s 前 > 0.5s 窗
    assert _player_spoke_recently(1001.0, now) is False          # 未來時戳（防呆）


@pytest.mark.asyncio
async def test_broadcast_news_noop_when_no_musiccog(monkeypatch):
    """沒有 MusicCog → 不查興趣關鍵字，直接查熱門頭條（不 raise）。"""
    from cogs.voice_controller import VoiceController

    async def _fake_fetch(keyword=None, **kw):
        assert keyword is None
        return {"title": "頭條新聞"}

    monkeypatch.setattr("news_fetch.fetch_news_headline", _fake_fetch)

    vc = VoiceController.__new__(VoiceController)
    vc.bot = MagicMock()
    vc.bot.cogs.get.return_value = None
    vc.play_tts = AsyncMock()

    await vc._maybe_broadcast_news()
    vc.play_tts.assert_awaited_once()
    assert "頭條新聞" in vc.play_tts.call_args[0][0]


@pytest.mark.asyncio
async def test_broadcast_news_uses_interest_keyword(monkeypatch):
    from cogs.voice_controller import VoiceController

    async def _fake_fetch(keyword=None, **kw):
        assert keyword == "F1賽車"
        return {"title": "F1 新賽季開跑"}

    monkeypatch.setattr("news_fetch.fetch_news_headline", _fake_fetch)

    vc = VoiceController.__new__(VoiceController)
    vc.bot = MagicMock()
    mc = MagicMock()
    mc._present_interests.return_value = ["小明喜歡F1賽車"]
    vc.bot.cogs.get.return_value = mc
    vc.play_tts = AsyncMock()

    await vc._maybe_broadcast_news()
    vc.play_tts.assert_awaited_once()
    assert "F1 新賽季開跑" in vc.play_tts.call_args[0][0]


@pytest.mark.asyncio
async def test_broadcast_news_silent_when_no_headline(monkeypatch):
    """抓不到新聞（None）→ 靜靜跳過，不呼叫 play_tts。"""
    from cogs.voice_controller import VoiceController

    async def _fake_fetch(keyword=None, **kw):
        return None

    monkeypatch.setattr("news_fetch.fetch_news_headline", _fake_fetch)

    vc = VoiceController.__new__(VoiceController)
    vc.bot = MagicMock()
    vc.bot.cogs.get.return_value = None
    vc.play_tts = AsyncMock()

    await vc._maybe_broadcast_news()
    vc.play_tts.assert_not_awaited()


@pytest.mark.asyncio
async def test_broadcast_news_sets_cooldown_timestamp_before_fetch():
    """冷卻時戳在呼叫前就先蓋，避免抓取卡住時同一輪 tick 重入。"""
    import time as time_mod
    from cogs.voice_controller import VoiceController

    vc = VoiceController.__new__(VoiceController)
    vc.bot = MagicMock()
    vc.bot.cogs.get.return_value = None
    vc.play_tts = AsyncMock()

    before = time_mod.time()
    await vc._maybe_broadcast_news()
    assert vc._last_news_broadcast_ts >= before


@pytest.mark.asyncio
async def test_broadcast_news_uses_raw_interest_keyword(monkeypatch):
    """如果興趣字串沒有『喜歡』前綴（例如純字串），也能正確作為關鍵字查詢。"""
    from cogs.voice_controller import VoiceController

    async def _fake_fetch(keyword=None, **kw):
        assert keyword == "深度學習"
        return {"title": "AI 最新突破"}

    monkeypatch.setattr("news_fetch.fetch_news_headline", _fake_fetch)

    vc = VoiceController.__new__(VoiceController)
    vc.bot = MagicMock()
    mc = MagicMock()
    mc._present_interests.return_value = ["深度學習"]
    vc.bot.cogs.get.return_value = mc
    vc.play_tts = AsyncMock()

    await vc._maybe_broadcast_news()
    vc.play_tts.assert_awaited_once()
    assert "AI 最新突破" in vc.play_tts.call_args[0][0]

