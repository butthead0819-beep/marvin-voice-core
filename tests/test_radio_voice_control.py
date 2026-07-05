"""
TDD：語音控制 marvin_radio 播放行為

測試語音指令 stop / pause / resume / skip 對 radio_mode 的影響。
"""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock


def _make_cog():
    """_handle_voice_music_command 已移至 MusicCog，透過 vc_mock 提供 VC 依賴。"""
    bot = MagicMock()
    bot.guilds = []
    bot.music_memory = None

    # vc_mock 提供 MC._handle_voice_music_command 需要的 VC 屬性
    vc_mock = MagicMock()
    _placeholder_msg = MagicMock()
    _placeholder_msg.edit = AsyncMock()
    _placeholder_msg.delete = AsyncMock()
    vc_mock.active_text_channel = AsyncMock()
    vc_mock.active_text_channel.send = AsyncMock(return_value=_placeholder_msg)
    vc_mock.stt_logger = MagicMock()
    vc_mock._play_ack = AsyncMock()
    vc_mock._extract_music_search_query = MagicMock(return_value="query")

    def _cogs_get(name):
        if name == 'VoiceController':
            return vc_mock
        return None

    bot.cogs.get.side_effect = _cogs_get

    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    cog.stream_queue = []
    cog.stream_history = []
    cog.stream_mode = False
    cog.radio_mode = False
    cog.radio_paused = False
    cog.stream_paused = False
    cog._vc_mock = vc_mock  # 暴露給測試斷言用
    return cog


def _make_vc(playing: bool = True, paused: bool = False) -> MagicMock:
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = playing
    vc.is_paused.return_value = paused
    vc.pause = MagicMock()
    vc.resume = MagicMock()
    vc.stop_playing = MagicMock()
    return vc


# ── stop ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_voice_stop_radio_mode_calls_stop_radio():
    """stop 指令：只有 radio_mode 開啟時，應呼叫 stop_radio() 並貼成功訊息。"""
    cog = _make_cog()
    cog.radio_mode = True
    cog.stream_mode = False
    cog.stop_radio = AsyncMock()

    await cog._handle_voice_music_command("狗與露", "停止播放", "stop")

    cog.stop_radio.assert_awaited_once()
    cog._vc_mock.active_text_channel.send.assert_called_once()
    sent = cog._vc_mock.active_text_channel.send.call_args[0][0]
    assert "⏹️" in sent


@pytest.mark.asyncio
async def test_voice_stop_no_modes_active_sends_error():
    """stop 指令：radio_mode 和 stream_mode 均關閉時，回傳錯誤提示。"""
    cog = _make_cog()
    cog.radio_mode = False
    cog.stream_mode = False
    cog.stop_radio = AsyncMock()
    cog.stop_stream = AsyncMock()

    await cog._handle_voice_music_command("狗與露", "停止播放", "stop")

    cog.stop_radio.assert_not_awaited()
    cog.stop_stream.assert_not_awaited()
    cog._vc_mock.active_text_channel.send.assert_called_once()
    sent = cog._vc_mock.active_text_channel.send.call_args[0][0]
    assert "😑" in sent


@pytest.mark.asyncio
async def test_voice_stop_both_modes_active_stops_both():
    """stop 指令：radio_mode 和 stream_mode 同時開啟時，兩者都要停止。"""
    cog = _make_cog()
    cog.radio_mode = True
    cog.stream_mode = True
    cog.stop_radio = AsyncMock()
    cog.stop_stream = AsyncMock()

    await cog._handle_voice_music_command("狗與露", "停止播放", "stop")

    cog.stop_radio.assert_awaited_once()
    cog.stop_stream.assert_awaited_once()
    cog._vc_mock.active_text_channel.send.assert_called_once()


# ── pause ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_voice_pause_radio_mode_pauses_vc():
    """pause 指令：只有 radio_mode 開啟時，應 pause mixer 並設定 radio_paused=True。"""
    cog = _make_cog()
    cog._vc_mock._mixer = MagicMock()
    discord_vc = _make_vc()
    cog.bot.voice_clients = [discord_vc]
    cog.radio_mode = True
    cog.radio_paused = False
    cog.stream_mode = False

    await cog._handle_voice_music_command("狗與露", "暫停播放", "pause")

    cog._vc_mock._mixer.set_paused.assert_called_once_with(True)
    assert cog.radio_paused is True
    cog._vc_mock.active_text_channel.send.assert_called_once()
    sent = cog._vc_mock.active_text_channel.send.call_args[0][0]
    assert "⏸️" in sent


@pytest.mark.asyncio
async def test_voice_pause_radio_already_paused_no_double_pause():
    """pause 指令：radio_paused=True 時不應重複呼叫 mixer.set_paused()。"""
    cog = _make_cog()
    cog._vc_mock._mixer = MagicMock()
    discord_vc = _make_vc(playing=False, paused=True)
    cog.bot.voice_clients = [discord_vc]
    cog.radio_mode = True
    cog.radio_paused = True
    cog.stream_mode = False

    await cog._handle_voice_music_command("狗與露", "暫停播放", "pause")

    cog._vc_mock._mixer.set_paused.assert_not_called()


@pytest.mark.asyncio
async def test_voice_pause_no_modes_active_sends_error():
    """pause 指令：radio_mode 和 stream_mode 均關閉時，回傳錯誤提示。"""
    cog = _make_cog()
    discord_vc = _make_vc()
    cog.bot.voice_clients = [discord_vc]
    cog.radio_mode = False
    cog.stream_mode = False

    await cog._handle_voice_music_command("狗與露", "暫停播放", "pause")

    cog._vc_mock.active_text_channel.send.assert_called_once()
    sent = cog._vc_mock.active_text_channel.send.call_args[0][0]
    assert "😑" in sent


# ── resume ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_voice_resume_radio_paused_resumes_vc():
    """resume 指令：radio_paused=True 時應 resume mixer 並清除 radio_paused。"""
    cog = _make_cog()
    cog._vc_mock._mixer = MagicMock()
    discord_vc = _make_vc(playing=False, paused=True)
    cog.bot.voice_clients = [discord_vc]
    cog.radio_mode = True
    cog.radio_paused = True
    cog.stream_mode = False
    cog.stream_paused = False

    await cog._handle_voice_music_command("狗與露", "繼續播", "resume")

    cog._vc_mock._mixer.set_paused.assert_called_once_with(False)
    assert cog.radio_paused is False
    cog._vc_mock.active_text_channel.send.assert_called_once()
    sent = cog._vc_mock.active_text_channel.send.call_args[0][0]
    assert "▶️" in sent


@pytest.mark.asyncio
async def test_voice_resume_no_modes_paused_sends_error():
    """resume 指令：radio_paused 和 stream_paused 均為 False 時，回傳錯誤提示。"""
    cog = _make_cog()
    discord_vc = _make_vc()
    cog.bot.voice_clients = [discord_vc]
    cog.radio_mode = True
    cog.radio_paused = False
    cog.stream_mode = False
    cog.stream_paused = False

    await cog._handle_voice_music_command("狗與露", "繼續播", "resume")

    cog._vc_mock.active_text_channel.send.assert_called_once()
    sent = cog._vc_mock.active_text_channel.send.call_args[0][0]
    assert "😑" in sent


# ── skip ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_voice_skip_radio_mode_stops_vc():
    """skip 指令：radio_mode 開啟時應呼叫 mixer.clear_music()，觸發換下一首。"""
    cog = _make_cog()
    cog._vc_mock._mixer = MagicMock()
    discord_vc = _make_vc()
    cog.bot.voice_clients = [discord_vc]
    cog.radio_mode = True
    cog.stream_mode = False

    await cog._handle_voice_music_command("狗與露", "下一首", "skip")

    cog._vc_mock._mixer.clear_music.assert_called_once()
    cog._vc_mock.active_text_channel.send.assert_called_once()
    sent = cog._vc_mock.active_text_channel.send.call_args[0][0]
    assert "⏭️" in sent


@pytest.mark.asyncio
async def test_voice_skip_no_modes_active_sends_error():
    """skip 指令：radio_mode 和 stream_mode 均關閉時，回傳錯誤提示。"""
    cog = _make_cog()
    discord_vc = _make_vc()
    cog.bot.voice_clients = [discord_vc]
    cog.radio_mode = False
    cog.stream_mode = False

    await cog._handle_voice_music_command("狗與露", "下一首", "skip")

    cog._vc_mock.active_text_channel.send.assert_called_once()
    sent = cog._vc_mock.active_text_channel.send.call_args[0][0]
    assert "😑" in sent


# ── 本機模式（無 Discord VC）音樂播放接縫 ──────────────────────────────────────

@pytest.mark.asyncio
async def test_voice_play_local_mode_does_not_require_discord_vc():
    """本機模式：vc._resolve_playback_device() 回本機喇叭（非 Discord VC）→ play 指令
    不該回『先用 /summon』bail，應繼續搜尋。修 music_cog 寫死 Discord VC 導致本機無聲。"""
    cog = _make_cog()
    cog.stream_mode = False
    cog.radio_mode = False
    cog.bot.voice_clients = []                          # 無 Discord VC（本機模式）
    cog._vc_mock._resolve_playback_device = MagicMock(return_value=MagicMock())  # 本機喇叭
    cog._resolve_yt_query = AsyncMock(return_value=None)  # 搜尋回 None，止於 resolve（不真播）

    await cog._handle_voice_music_command("狗與露", "播放周杰倫", "play")

    sends = [c.args[0] for c in cog._vc_mock.active_text_channel.send.call_args_list]
    assert not any("summon" in s for s in sends), f"本機模式不該要求 summon: {sends}"
    assert any("搜尋" in s for s in sends), f"應進到搜尋: {sends}"


@pytest.mark.asyncio
async def test_voice_play_no_device_at_all_still_bails():
    """對照：連本機喇叭都沒有（_resolve_playback_device→None）→ 照舊 bail 要求 summon。"""
    cog = _make_cog()
    cog.stream_mode = False
    cog.radio_mode = False
    cog.bot.voice_clients = []
    cog._vc_mock._resolve_playback_device = MagicMock(return_value=None)

    await cog._handle_voice_music_command("狗與露", "播放周杰倫", "play")

    sends = [c.args[0] for c in cog._vc_mock.active_text_channel.send.call_args_list]
    assert any("summon" in s for s in sends), f"無任何裝置時應要求 summon: {sends}"
