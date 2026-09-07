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
async def test_play_tts_stream_mode_mutes_without_bypass():
    """既有行為基準：stream_mode=True + silent_during_stream=True → Stream Guard 靜音，
    不進 mixer（沒有 bypass_stream_mute 時的既有行為，不該被下面的 bypass 測試 regress）。"""
    cog, _ = _make_cog()
    cog.stream_mode = True
    device = MagicMock(spec=DiscordPlaybackDevice)

    with patch.object(cog, "_resolve_playback_device", return_value=device):
        await cog.play_tts("測試", silent_during_stream=True)

    cog._stream_tts_to_mixer.assert_not_called()


@pytest.mark.asyncio
async def test_play_tts_bypass_stream_mute_speaks_over_music():
    """插播新聞：bypass_stream_mute=True → 即使 stream_mode(放音樂/直播中) 也要唸出來，
    不被 Stream Guard 靜音（進 mixer 後交給既有 duck 機制自動壓低音樂音量）。"""
    cog, _ = _make_cog()
    cog.stream_mode = True
    device = MagicMock(spec=DiscordPlaybackDevice)

    with patch.object(cog, "_resolve_playback_device", return_value=device):
        await cog.play_tts("狗與露上線了", silent_during_stream=True, bypass_stream_mute=True)

    cog._stream_tts_to_mixer.assert_called_once()


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


# ── Interrupt Guard × protected ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_play_tts_interrupt_guard_still_drops_unprotected():
    """既有行為基準：_tts_interrupted=True + already_in_channel=True + 非 protected
    → Interrupt Guard 丟句，不進 mixer。"""
    cog, _ = _make_cog()
    cog._tts_interrupted = True
    cog._tts_protected = False
    device = MagicMock(spec=DiscordPlaybackDevice)

    with patch.object(cog, "_resolve_playback_device", return_value=device):
        await cog.play_tts("殘句", already_in_channel=True)

    cog._stream_tts_to_mixer.assert_not_called()


@pytest.mark.asyncio
async def test_play_tts_protected_clears_stale_interrupt_and_speaks():
    """進場招呼修：protected TTS 是獨立完整 unit，遇到上一句留下的 _tts_interrupted
    不該被 Interrupt Guard 吃掉 —— 清旗標並照唸（實測 showay 進場招呼被丟的根因）。"""
    cog, _ = _make_cog()
    cog._tts_interrupted = True   # 上一句被使用者打斷留下的陳年旗標
    cog._tts_protected = True
    device = MagicMock(spec=DiscordPlaybackDevice)

    with patch.object(cog, "_resolve_playback_device", return_value=device):
        await cog.play_tts("狗與露 登台，歌單打開，今晚想聽什麼交給他安排！", already_in_channel=True)

    cog._stream_tts_to_mixer.assert_called_once()
    assert cog._tts_interrupted is False, "protected 播放前應清掉殘留的中斷旗標"


@pytest.mark.asyncio
async def test_speak_protected_sets_and_restores_flag_and_speaks_over_stale_interrupt():
    """進場招呼路徑：speak(protected=True) 要自己把 self._tts_protected 拉起來
    （kwarg 是死的）→ play_tts 的 Interrupt Guard 不再被陳年 _tts_interrupted 擋，
    句子照進 mixer；播完旗標還原。"""
    cog, _ = _make_cog()
    cog._tts_interrupted = True   # 上一句被打斷的陳年旗標
    cog._tts_protected = False
    device = MagicMock(spec=DiscordPlaybackDevice)

    with patch.object(cog, "_resolve_playback_device", return_value=device):
        await cog.speak("showay 一到，手把抓牢，工作的事今晚一律不聊！",
                        proactive=True, protected=True, bypass_stream_mute=True)

    cog._stream_tts_to_mixer.assert_called_once()
    assert cog._tts_protected is False, "speak 播完應還原 _tts_protected"


# ── Hot-Chat Guard × protected ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_play_tts_hot_chat_still_mutes_unprotected_proactive():
    """既有行為基準：熱聊中 + silent_during_stream + 非 protected → Hot-Chat Mute 丟句。"""
    cog, _ = _make_cog()
    cog._tts_protected = False
    cog._room_mood_store = MagicMock()
    cog._room_mood_store.get.return_value = MagicMock(hot_chat=True)
    device = MagicMock(spec=DiscordPlaybackDevice)

    with patch.object(cog, "_resolve_playback_device", return_value=device):
        await cog.play_tts("閒聊一句", silent_during_stream=True)

    cog._stream_tts_to_mixer.assert_not_called()


@pytest.mark.asyncio
async def test_play_tts_protected_speaks_through_hot_chat():
    """進場招呼修：protected 是要插播的獨立短事件 → 熱聊中照唸、不被 Hot-Chat Mute
    吃掉（實測 狗與露 進場招呼被 🦆 Hot-Chat Mute 丟的根因）。"""
    cog, _ = _make_cog()
    cog._tts_protected = True
    cog._room_mood_store = MagicMock()
    cog._room_mood_store.get.return_value = MagicMock(hot_chat=True)
    device = MagicMock(spec=DiscordPlaybackDevice)

    with patch.object(cog, "_resolve_playback_device", return_value=device):
        await cog.play_tts("狗與露 登台，歌單打開，今晚想聽什麼交給他安排！",
                           silent_during_stream=True, already_in_channel=True)

    cog._stream_tts_to_mixer.assert_called_once()


# ── _tts_suppressed：單一守門，protected 一次跳過全部 ─────────────────────────

@pytest.mark.asyncio
async def test_tts_suppressed_committed_bypasses_every_guard_at_once():
    """契約：committed kind（或 protected=True）時，就算 game_mode + stream_mode +
    熱聊 + 佇列爆 + 陳年中斷全部成立，_tts_suppressed 仍回 False（且清 _tts_interrupted）。
    規則全在 tts_speak_policy.decide()，這裡只驗接線。"""
    from tts_speak_policy import SpeakKind

    cog, _ = _make_cog()
    cog._tts_protected = False
    cog.game_mode = True
    cog.stream_mode = True
    cog._tts_interrupted = True
    cog._room_mood_store = MagicMock()
    cog._room_mood_store.get.return_value = MagicMock(hot_chat=True)
    cog._mixer.tts_load_seconds.return_value = 999.0

    suppressed = await cog._tts_suppressed(
        text="showay 一到，手把抓牢，工作的事今晚一律不聊！",
        silent_during_stream=True, already_in_channel=True, bypass_stream_mute=False,
        kind=SpeakKind.JOIN_GREETING,
    )

    assert suppressed is False
    assert cog._tts_interrupted is False


@pytest.mark.asyncio
async def test_tts_suppressed_load_drop_still_gated_for_unprotected():
    """既有行為基準：非 committed + mixer 佇列積壓超上限 → 丟句（補文字）。"""
    cog, _ = _make_cog()
    cog._tts_protected = False
    cog._mixer.tts_load_seconds.return_value = 999.0

    suppressed = await cog._tts_suppressed(
        text="一句被積壓丟掉的話", silent_during_stream=False,
        already_in_channel=True, bypass_stream_mute=False,
    )

    assert suppressed is True


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
