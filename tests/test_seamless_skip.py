"""
TDD: 無縫切歌 (Seamless Skip with DJ Interjection) 測試

驗證手動 skip 立即生效（clear_music 不等 DJ 串場），DJ 串場改背景執行、
逾時或出錯都不影響已經生效的 skip（2026-08-06：原本 await 到 DJ meta 解析/
播放完才切歌，逼近 10s 逾時時使用者會覺得指令沒反應）。
"""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock


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
async def test_seamless_skip_clears_immediately_without_waiting_for_dj():
    """當佇列有下一首時，skip 立即呼叫 clear_music，不等 DJ meta 解析完成。"""
    cog = _make_cog()
    next_info = {"title": "夜曲", "url": "http://example.com/song2", "duration": 200}
    cog.stream_queue = [next_info]

    preload_called = False
    meta_started = asyncio.Event()
    release_meta = asyncio.Event()

    def _mock_preload(info):
        nonlocal preload_called
        preload_called = True

    async def _mock_resolve_dj(info, *args, **kwargs):
        meta_started.set()
        # 模擬長時間才會回來的 DJ 解析——不該卡住 skip 本身
        await release_meta.wait()
        return {"text": "下一首是夜曲", "audio_path": None}

    cog._start_music_preload = MagicMock(side_effect=_mock_preload)
    cog._resolve_tail_dj_meta = AsyncMock(side_effect=_mock_resolve_dj)
    cog._maybe_play_dj_interjection = AsyncMock()

    # 執行 skip 指令：即使背景 DJ 解析還沒回來，指令本身要馬上完成
    await asyncio.wait_for(cog._handle_voice_music_command("UserA", "", "skip"), timeout=1.0)

    assert preload_called is True
    cog._vc_mock._mixer.clear_music.assert_called_once()

    # 背景 DJ 串場之後才完成解析與播放
    release_meta.set()
    await asyncio.sleep(0.05)
    cog._maybe_play_dj_interjection.assert_awaited_once()
    assert next_info.get('_dj_played_in_tail') is True


@pytest.mark.asyncio
async def test_seamless_skip_dj_background_timeout_does_not_affect_skip():
    """DJ 背景解析逾時（極端情況 >10s）只影響背景串場，不影響已經生效的 skip。"""
    cog = _make_cog()
    next_info = {"title": "慢歌", "url": "http://example.com/song_slow", "duration": 200}
    cog.stream_queue = [next_info]

    async def _slow_resolve_dj(info):
        # 模擬非常卡頓的抓取
        await asyncio.sleep(100)
        return None

    cog._start_music_preload = MagicMock()
    cog._resolve_tail_dj_meta = AsyncMock(side_effect=_slow_resolve_dj)
    cog._maybe_play_dj_interjection = AsyncMock()
    cog._SEAMLESS_SKIP_TIMEOUT_S = 0.1  # 測試時覆寫為短逾時

    # skip 本身不等背景任務，應立即完成
    await asyncio.wait_for(cog._handle_voice_music_command("UserA", "", "skip"), timeout=1.0)
    cog._vc_mock._mixer.clear_music.assert_called_once()

    # 背景任務逾時後自行放棄，不會播放 DJ 串場
    await asyncio.sleep(0.2)
    cog._maybe_play_dj_interjection.assert_not_awaited()


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

