"""marvin_speech_log 驗收：每句實際播出的 TTS 完整台詞 + 開始時間寫進獨立 log。

規則：只 import 被測程式碼，不把被測邏輯複製進測試裡驗算自己。
"""
from __future__ import annotations

import asyncio
import json
import logging

from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from marvin_speech_log import LOGGER_NAME, configure_marvin_speech_logger, log_marvin_speech


def _last_json_line(path):
    with open(path, "r", encoding="utf-8") as f:
        lines = [l for l in f.read().splitlines() if l.strip()]
    assert lines, f"{path} 沒有任何一行"
    return json.loads(lines[-1])


def _teardown_logger(handler):
    logger = logging.getLogger(LOGGER_NAME)
    logger.removeHandler(handler)
    handler.close()


# ── 1-3. log_marvin_speech 基本行為 ──────────────────────────────────────────

def test_log_marvin_speech_writes_full_text_and_fields(tmp_path):
    log_path = tmp_path / "x.log"
    handler = configure_marvin_speech_logger(str(log_path))
    try:
        text = "一" * 80
        log_marvin_speech(text, start_ts=1790000000.1234, layer=2,
                           voice="en-US-GuyNeural", src="tts")
        handler.flush()

        row = _last_json_line(log_path)
        assert len(row["text"]) == 80
        assert row["start"] == 1790000000.123
        assert row["layer"] == 2
        assert row["voice"] == "en-US-GuyNeural"
        assert row["src"] == "tts"
    finally:
        _teardown_logger(handler)


def test_log_marvin_speech_collapses_newlines_to_space(tmp_path):
    log_path = tmp_path / "x.log"
    handler = configure_marvin_speech_logger(str(log_path))
    try:
        log_marvin_speech("第一行\n第二行", start_ts=1.0, layer=1, voice=None, src="tts")
        handler.flush()
        row = _last_json_line(log_path)
        assert row["text"] == "第一行 第二行"
    finally:
        _teardown_logger(handler)


def test_log_marvin_speech_skips_blank_text(tmp_path):
    log_path = tmp_path / "x.log"
    handler = configure_marvin_speech_logger(str(log_path))
    try:
        log_marvin_speech("  \n ", start_ts=1.0, layer=1, voice=None, src="tts")
        handler.flush()
        assert not log_path.exists() or log_path.read_text(encoding="utf-8").strip() == ""
    finally:
        _teardown_logger(handler)


# ── 4-5. configure_marvin_speech_logger 冪等 + propagate ────────────────────

def test_configure_twice_same_path_installs_one_handler(tmp_path):
    log_path = tmp_path / "x.log"
    h1 = configure_marvin_speech_logger(str(log_path))
    h2 = configure_marvin_speech_logger(str(log_path))
    try:
        assert h1 is h2
        logger = logging.getLogger(LOGGER_NAME)
        matching = [h for h in logger.handlers if getattr(h, "baseFilename", None) == h1.baseFilename]
        assert len(matching) == 1
    finally:
        _teardown_logger(h1)


def test_logger_does_not_propagate(tmp_path):
    log_path = tmp_path / "x.log"
    handler = configure_marvin_speech_logger(str(log_path))
    try:
        assert logging.getLogger(LOGGER_NAME).propagate is False
    finally:
        _teardown_logger(handler)


# ── 6-7. _stream_tts_to_mixer 首幀記錄 ───────────────────────────────────────

def _make_cog_real_stream_tts():
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


def _fake_stream_audio_empty():
    async def _gen(*args, **kwargs):
        return
        yield  # noqa: unreachable
    return MagicMock(side_effect=_gen)


def _fake_ffmpeg_proc_one_frame():
    from local_mixing_source import FRAME_BYTES_F32
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    proc.stdout.readexactly = AsyncMock(side_effect=[
        b"\x00" * FRAME_BYTES_F32,
        asyncio.IncompleteReadError(b"", 0),
    ])
    return proc


def _fake_ffmpeg_proc_no_frame():
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    proc.stdout.readexactly = AsyncMock(side_effect=asyncio.IncompleteReadError(b"", 0))
    return proc


@pytest.mark.asyncio
async def test_stream_tts_to_mixer_logs_full_text_on_first_frame():
    cog = _make_cog_real_stream_tts()
    cog.bot.tts_engine.stream_audio = _fake_stream_audio_empty()
    long_text = "這是一句超過三十個字的測試台詞，用來確認完整原文有被寫進 marvin_speech.log 而不是被截斷成三十字。"
    assert len(long_text) > 30

    create_subprocess_mock = AsyncMock(return_value=_fake_ffmpeg_proc_one_frame())
    with patch("asyncio.create_subprocess_exec", create_subprocess_mock), \
         patch("cogs.voice_controller_playback.log_marvin_speech") as mock_log:
        await cog._stream_tts_to_mixer(
            long_text, force_macos=False, emotion_tag="neutral", voice="zh-TW-YunJheNeural", layer=1,
        )

    mock_log.assert_called_once()
    _, kwargs = mock_log.call_args
    assert mock_log.call_args.args[0] == long_text
    assert kwargs["src"] == "tts"
    assert kwargs["layer"] == 1
    assert kwargs["voice"] == "zh-TW-YunJheNeural"
    assert isinstance(kwargs["start_ts"], float)


@pytest.mark.asyncio
async def test_stream_tts_to_mixer_no_frame_does_not_log():
    cog = _make_cog_real_stream_tts()
    cog.bot.tts_engine.stream_audio = _fake_stream_audio_empty()

    create_subprocess_mock = AsyncMock(return_value=_fake_ffmpeg_proc_no_frame())
    with patch("asyncio.create_subprocess_exec", create_subprocess_mock), \
         patch("cogs.voice_controller_playback.log_marvin_speech") as mock_log:
        await cog._stream_tts_to_mixer(
            "不會被推播的文字", force_macos=False, emotion_tag="neutral", voice=None,
        )

    mock_log.assert_not_called()


# ── 8. play_dj_on_tts_layer ──────────────────────────────────────────────────

def _make_light_dj_cog():
    from cogs.voice_controller_playback import PlaybackMixin

    class _Cog:
        play_dj_on_tts_layer = PlaybackMixin.play_dj_on_tts_layer

    cog = _Cog()
    cog._ffmpeg_to_f32 = AsyncMock(return_value=np.array([0.1, 0.2], dtype=np.float32))
    cog._ensure_mixer_playing = MagicMock()
    cog._resolve_playback_device = MagicMock(return_value=MagicMock())
    cog._mixer = MagicMock()
    return cog


@pytest.mark.asyncio
async def test_play_dj_on_tts_layer_logs_on_success_with_text():
    cog = _make_light_dj_cog()
    cog._mixer.push_tts = MagicMock(return_value=True)

    with patch("os.path.exists", return_value=True), \
         patch("cogs.voice_controller_playback.log_marvin_speech") as mock_log:
        result = await cog.play_dj_on_tts_layer("fake.mp3", text="DJ 口白")

    assert result is True
    mock_log.assert_called_once()
    args, kwargs = mock_log.call_args
    assert args[0] == "DJ 口白"
    assert kwargs["src"] == "dj"


@pytest.mark.asyncio
async def test_play_dj_on_tts_layer_does_not_log_when_push_fails():
    cog = _make_light_dj_cog()
    cog._mixer.push_tts = MagicMock(return_value=False)

    with patch("os.path.exists", return_value=True), \
         patch("cogs.voice_controller_playback.log_marvin_speech") as mock_log:
        result = await cog.play_dj_on_tts_layer("fake.mp3", text="DJ 口白")

    assert result is False
    mock_log.assert_not_called()


@pytest.mark.asyncio
async def test_play_dj_on_tts_layer_no_text_no_log_for_sfx():
    cog = _make_light_dj_cog()
    cog._mixer.push_tts = MagicMock(return_value=True)

    with patch("os.path.exists", return_value=True), \
         patch("audio_mixing.peak_normalize_f32", return_value=np.array([0.1], dtype=np.float32)), \
         patch("cogs.voice_controller_playback.log_marvin_speech") as mock_log:
        result = await cog.play_dj_on_tts_layer("fake.wav", peak=0.1)

    assert result is True
    mock_log.assert_not_called()


# ── 9. _maybe_play_dj_interjection 傳 text ──────────────────────────────────

def _make_music_cog_for_interjection():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.tts_engine = MagicMock()

    vc = MagicMock()
    vc._intimate_mode = False
    vc.play_dj_on_tts_layer = AsyncMock(return_value=True)
    import contextlib
    vc._protected_tts_window = MagicMock(side_effect=lambda: contextlib.nullcontext())
    bot.cogs.get.return_value = vc

    from cogs.music_cog import MusicCog
    cog = MusicCog(bot)
    cog._enable_dj_news_fetch = False
    return cog, vc


@pytest.mark.asyncio
async def test_maybe_play_dj_interjection_passes_text_through(tmp_path):
    cog, vc = _make_music_cog_for_interjection()
    audio_path = tmp_path / "dj.mp3"
    audio_path.write_bytes(b"x")
    dj = {"text": "這首歌是狗與露點的", "audio_path": str(audio_path)}

    with patch("os.path.exists", return_value=True), \
         patch("cogs.music_cog._get_puck_client", return_value=None):
        await cog._maybe_play_dj_interjection(dj)

    vc.play_dj_on_tts_layer.assert_awaited_once()
    _, kwargs = vc.play_dj_on_tts_layer.call_args
    assert kwargs["text"] == "這首歌是狗與露點的"


# ── 10. _speak_song_ack 把「幫你點了《X》」原稿傳進 play_dj_on_tts_layer ─────────

@pytest.mark.asyncio
async def test_speak_song_ack_passes_ack_text_through():
    cog, vc = _make_music_cog_for_interjection()
    cog.bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/ack.mp3")

    await cog._speak_song_ack(vc, "七里香")

    cog.bot.tts_engine.generate_audio.assert_awaited_once_with("幫你點了《七里香》")
    vc.play_dj_on_tts_layer.assert_awaited_once()
    _, kwargs = vc.play_dj_on_tts_layer.call_args
    assert kwargs["text"] == "幫你點了《七里香》"
