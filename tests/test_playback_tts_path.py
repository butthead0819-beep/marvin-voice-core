"""TDD — ③b: TTS 路徑改走 PlaybackDevice (_resolve_playback_device)。

先紅後綠：
  - test_*_calls_resolve_playback_device：改前 _resolve 不被呼叫 → assert_called* fails → RED
  - test_*_returns_*_when_resolve_returns_none：改前 old next() 找到 vc 繼續執行；
    _resolve 回 None 是 patch 的，old code 無視 → 行為與 None 不同 → RED

改後全綠。
"""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from marvin_voice_core.playback_device import DiscordPlaybackDevice


def _make_cog_real_stream_tts():
    """VoiceController *不* mock _stream_tts_to_mixer（用來測 volume threading）。"""
    bot = MagicMock()
    bot.guilds = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()

    with patch("discord_voice_engine.faster_whisper", None, create=True):
        from discord_voice_engine import DiscordVoiceEngine
        engine = DiscordVoiceEngine(bot)
    bot.engine = engine

    with patch("discord.ext.tasks.loop", lambda *a, **kw: lambda f: f), \
         patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        cog = VoiceController(bot)

    cog._tts_interrupted = False
    cog._mixer = MagicMock()
    cog._mixer.push_tts = MagicMock()
    cog._mixer.push_tts2 = MagicMock()
    return cog


def _make_cog():
    """Minimal VoiceController for playback path tests (same pattern as test_tts_storm_fallback)."""
    bot = MagicMock()
    bot.guilds = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.get_estimated_duration.return_value = 0.01

    vc = MagicMock()
    vc.is_connected.return_value = True
    bot.voice_clients = [vc]

    with patch("discord_voice_engine.faster_whisper", None, create=True):
        from discord_voice_engine import DiscordVoiceEngine
        engine = DiscordVoiceEngine(bot)
    bot.engine = engine

    with patch("discord.ext.tasks.loop", lambda *a, **kw: lambda f: f), \
         patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        cog = VoiceController(bot)

    cog.active_text_channel = AsyncMock()
    cog.active_text_channel.send = AsyncMock()
    cog.game_mode = False
    cog._tts_protected = False
    cog._tts_interrupted = False
    cog._tts_flush_requested = False
    cog.stream_mode = False
    cog.radio_mode = False
    cog.is_playing_audio = False
    cog.tts_queue_duration = 0.0

    cog._mixer = MagicMock()
    cog._mixer.tts_load_seconds.return_value = 0.0
    cog._stream_tts_to_mixer = AsyncMock(return_value=10)
    cog._ensure_mixer_playing = MagicMock(return_value=True)
    cog._wait_for_user_silence = AsyncMock(return_value=True)

    return cog, vc


# ── play_tts ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_play_tts_calls_resolve_playback_device():
    """③b: play_tts 走 _resolve_playback_device()，不再自己 next(bot.voice_clients)。"""
    cog, vc = _make_cog()
    device = MagicMock(spec=DiscordPlaybackDevice)

    with patch.object(cog, "_resolve_playback_device", return_value=device) as mock_resolve:
        await cog.play_tts("測試文字")

    mock_resolve.assert_called_once()


@pytest.mark.asyncio
async def test_play_tts_ensure_mixer_called_with_device():
    """③c-ii: _ensure_mixer_playing 收到的是 device 本身（raw_voice_client 接縫已移除）。"""
    cog, vc = _make_cog()
    device = MagicMock(spec=DiscordPlaybackDevice)

    with patch.object(cog, "_resolve_playback_device", return_value=device):
        await cog.play_tts("測試文字")

    cog._ensure_mixer_playing.assert_called_once_with(device)


@pytest.mark.asyncio
async def test_play_tts_returns_early_when_resolve_returns_none():
    """③b: _resolve 回 None → 提早 return，_stream_tts_to_mixer 不被呼叫。

    前置：bot.voice_clients 有連線 vc（old code 找得到），但 _resolve patch 回 None。
    改前：old next() 找到 vc → 繼續播 → _stream_tts_to_mixer 被呼叫 → RED。
    改後：_resolve → None → 提早 return → GREEN。
    """
    cog, _ = _make_cog()

    with patch.object(cog, "_resolve_playback_device", return_value=None):
        await cog.play_tts("測試文字")

    cog._stream_tts_to_mixer.assert_not_called()


# ── _play_dual_interject ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dual_interject_calls_resolve_playback_device():
    """③b: _play_dual_interject 走 _resolve_playback_device()，不再讀 self.voice_client。

    改前：self.voice_client 找到 bot.voice_clients 的 vc，_resolve 未被呼叫 → RED。
    改後：_resolve 被呼叫 → GREEN。測試含 ~0.5s 的 mixer time-loop，用 wait_for 框住。
    """
    cog, vc = _make_cog()
    device = MagicMock(spec=DiscordPlaybackDevice)

    segments = [{"voice": "marvin", "text": "時間"}, {"voice": "marmo", "text": "閉嘴"}]

    with patch.object(cog, "_resolve_playback_device", return_value=device) as mock_resolve:
        await asyncio.wait_for(cog._play_dual_interject(segments), timeout=3.0)

    mock_resolve.assert_called()


@pytest.mark.asyncio
async def test_dual_interject_returns_false_when_resolve_returns_none():
    """③b: _resolve 回 None → _play_dual_interject 立即回 False，_stream_tts_to_mixer 不呼叫。

    前置：bot.voice_clients 有連線 vc（old code 找得到），_resolve patch 回 None。
    改前：self.voice_client 找到 vc → 繼續執行（約 0.5s）→ 回 True → RED。
    改後：_resolve → None → 立即 False → GREEN。
    """
    cog, _ = _make_cog()

    segments = [{"voice": "marvin", "text": "時間"}, {"voice": "marmo", "text": "閉嘴"}]

    with patch.object(cog, "_resolve_playback_device", return_value=None):
        result = await asyncio.wait_for(cog._play_dual_interject(segments), timeout=3.0)

    assert result is False
    cog._stream_tts_to_mixer.assert_not_called()


# ── play_local_file ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_play_local_file_calls_resolve_playback_device(tmp_path):
    """③b: play_local_file 走 _resolve_playback_device()，不再自己 next(bot.voice_clients)。"""
    cog, vc = _make_cog()
    device = MagicMock(spec=DiscordPlaybackDevice)
    device.is_connected.return_value = True
    cog._mixer_play_music = AsyncMock()

    f = tmp_path / "test.mp3"
    f.write_bytes(b"fake audio")

    with patch.object(cog, "_resolve_playback_device", return_value=device) as mock_resolve:
        await cog.play_local_file(str(f))

    mock_resolve.assert_called_once()


@pytest.mark.asyncio
async def test_play_local_file_returns_early_when_resolve_returns_none(tmp_path):
    """③b: _resolve 回 None → play_local_file 提早 return，_mixer_play_music 不被呼叫。"""
    cog, _ = _make_cog()
    cog._mixer_play_music = AsyncMock()

    f = tmp_path / "test.mp3"
    f.write_bytes(b"fake audio")

    with patch.object(cog, "_resolve_playback_device", return_value=None):
        await cog.play_local_file(str(f))

    cog._mixer_play_music.assert_not_called()


# ── _stream_tts_to_mixer volume threading ─────────────────────────────────────

def _fake_stream_audio_empty():
    """空 async generator 讓 _feed 立即完成。"""
    async def _gen(*args, **kwargs):
        return
        yield  # noqa: unreachable — makes this an async generator function
    return MagicMock(side_effect=_gen)


def _fake_ffmpeg_proc():
    """Fake ffmpeg proc：_drain 遇到 IncompleteReadError(b'', 0) 立即退出。"""
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    proc.stdout.readexactly = AsyncMock(
        side_effect=asyncio.IncompleteReadError(b"", 0)
    )
    return proc


@pytest.mark.asyncio
async def test_stream_tts_to_mixer_passes_volume_intimate_agitated():
    """_stream_tts_to_mixer 親密模式 AGITATED tag → stream_audio 收到 volume='-20%'。"""
    cog = _make_cog_real_stream_tts()
    cog._intimate_mode = True

    stream_audio_mock = _fake_stream_audio_empty()
    cog.bot.tts_engine.stream_audio = stream_audio_mock

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_fake_ffmpeg_proc())):
        await cog._stream_tts_to_mixer(
            "測試", force_macos=False, emotion_tag="excited", voice=None
        )

    stream_audio_mock.assert_called_once()
    call_kwargs = stream_audio_mock.call_args.kwargs
    assert call_kwargs.get("volume") == "-20%", \
        f"AGITATED intimate 模式 volume 預期 '-20%'，實際 {call_kwargs}"


@pytest.mark.asyncio
async def test_stream_tts_to_mixer_passes_volume_none_discord_path():
    """_stream_tts_to_mixer 非親密（Discord 路徑）→ stream_audio 收到 volume=None。"""
    cog = _make_cog_real_stream_tts()
    # 不設 _intimate_mode（Discord 路徑），_resolve 回 _EMOTION_TTS_PARAMS（無 volume 欄位）

    stream_audio_mock = _fake_stream_audio_empty()
    cog.bot.tts_engine.stream_audio = stream_audio_mock

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_fake_ffmpeg_proc())):
        await cog._stream_tts_to_mixer(
            "測試", force_macos=False, emotion_tag="excited", voice=None
        )

    stream_audio_mock.assert_called_once()
    call_kwargs = stream_audio_mock.call_args.kwargs
    assert call_kwargs.get("volume") is None, \
        f"Discord 路徑 volume 預期 None，實際 {call_kwargs}"
