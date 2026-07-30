"""
TDD: 無縫切歌 (Seamless Skip with DJ Interjection) 測試

驗證切歌時第一首音樂不會斷音，而是等待下一首與 DJ 串場非同步準備完成後才執行 _mixer.clear_music()。
"""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.music_memory = None

    vc_mock = MagicMock()
    _placeholder_msg = MagicMock()
    _placeholder_msg.edit = AsyncMock()
    _placeholder_msg.delete = AsyncMock()
    vc_mock.active_text_channel = AsyncMock()
    vc_mock.active_text_channel.send = AsyncMock(return_value=_placeholder_msg)
    vc_mock.stt_logger = MagicMock()
    vc_mock._play_ack = AsyncMock()
    vc_mock._mixer = MagicMock()
    vc_mock._mixer.clear_music = MagicMock()
    vc_mock._resolve_playback_device.return_value = MagicMock()

    def _cogs_get(name):
        if name == 'VoiceController':
            return vc_mock
        if name == 'MusicCog':
            return cog
        return None

    bot.cogs.get.side_effect = _cogs_get

    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    cog.stream_queue = []
    cog.stream_history = []
    cog.stream_mode = True
    cog.radio_mode = False
    cog.radio_paused = False
    cog.stream_paused = False
    cog._vc_mock = vc_mock
    return cog


@pytest.mark.asyncio
async def test_seamless_skip_empty_queue_clears_immediately():
    """當佇列為空時，skip 直接呼叫 clear_music。"""
    cog = _make_cog()
    cog.stream_queue = []

    await cog._handle_voice_music_command("UserA", "", "skip")

    # 佇列為空時，clear_music 立刻被呼叫
    cog._vc_mock._mixer.clear_music.assert_called_once()


@pytest.mark.asyncio
async def test_seamless_skip_preloads_next_song_before_clearing():
    """當佇列有下一首時，skip 會先預載下一首/DJ，預載完成後才呼叫 clear_music。"""
    cog = _make_cog()
    next_info = {"title": "夜曲", "url": "http://example.com/song2", "duration": 200}
    cog.stream_queue = [next_info]

    preload_called = False
    meta_called = False

    def _mock_preload(info):
        nonlocal preload_called
        preload_called = True

    async def _mock_resolve_dj(info):
        nonlocal meta_called
        meta_called = True
        # 模擬需要小幅度時間抓取
        await asyncio.sleep(0.05)
        return {"text": "下一首是夜曲", "audio_path": None}

    cog._start_music_preload = MagicMock(side_effect=_mock_preload)
    cog._resolve_tail_dj_meta = AsyncMock(side_effect=_mock_resolve_dj)
    cog._maybe_play_dj_interjection = AsyncMock()

    # 執行 skip 指令
    await cog._handle_voice_music_command("UserA", "", "skip")

    # 驗證：預載與 DJ 嘗試被啟動，且 clear_music 在預載過後呼叫
    assert preload_called is True
    assert meta_called is True
    cog._vc_mock._mixer.clear_music.assert_called_once()
    assert next_info.get('_dj_played_in_tail') is True


@pytest.mark.asyncio
async def test_seamless_skip_timeout_safeguard():
    """當預載逾時 (極端情況 >10s) 時，Timeout 防護機制會在門檻到時強行執行 clear_music。"""
    cog = _make_cog()
    next_info = {"title": "慢歌", "url": "http://example.com/song_slow", "duration": 200}
    cog.stream_queue = [next_info]

    async def _slow_resolve_dj(info):
        # 模擬非常卡頓的抓取
        await asyncio.sleep(100)
        return None

    cog._start_music_preload = MagicMock()
    cog._resolve_tail_dj_meta = AsyncMock(side_effect=_slow_resolve_dj)
    cog._SEAMLESS_SKIP_TIMEOUT_S = 0.1  # 測試時覆寫為短逾時

    await cog._handle_voice_music_command("UserA", "", "skip")

    # 驗證即使 DJ 抓取逾時，保險機制仍會觸發 clear_music 切歌
    cog._vc_mock._mixer.clear_music.assert_called_once()


@pytest.mark.asyncio
async def test_voice_view_skip_delegates_to_music_cog_and_triggers_ack():
    """控制台按鈕 skip 應播放 quick ack TTS 並委派 MusicCog _safe_music_command。"""
    from cogs.voice_views import PlayControlView
    cog = _make_cog()
    cog._safe_music_command = AsyncMock()
    
    # 建立 view 與 mock controller
    bot = cog.bot
    controller = MagicMock()
    controller.bot = bot
    controller.play_tts = AsyncMock()
    view = PlayControlView(controller)
    
    vc = cog._vc_mock
    view._skip_current(vc)
    await asyncio.sleep(0.05)
    
    # 驗證叫用了 quick ack TTS
    controller.play_tts.assert_awaited_once_with("好，換", already_in_channel=False)

    # 驗證委派了 music_cog._safe_music_command("control_panel", "", "skip")
    cog._safe_music_command.assert_awaited_once()
    args = cog._safe_music_command.call_args[0]
    assert args[2] == "skip"

