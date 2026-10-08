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

from marvin_speech_log import LOGGER_NAME, configure_marvin_speech_logger, log_marvin_speech, log_song_start


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


# ── 5.1-5.5. log_song_start ──────────────────────────────────────────────────

def test_log_song_start_writes_start_src_text(tmp_path):
    log_path = tmp_path / "x.log"
    handler = configure_marvin_speech_logger(str(log_path))
    try:
        log_song_start("七里香", start_ts=1790000000.1234)
        handler.flush()
        row = _last_json_line(log_path)
        assert row["start"] == 1790000000.123
        assert row["src"] == "song"
        assert row["text"] == "七里香"
        assert "artist" not in row
    finally:
        _teardown_logger(handler)


def test_log_song_start_includes_artist_when_given(tmp_path):
    log_path = tmp_path / "x.log"
    handler = configure_marvin_speech_logger(str(log_path))
    try:
        log_song_start("七里香", start_ts=1.0, artist="周杰倫")
        handler.flush()
        row = _last_json_line(log_path)
        assert row["artist"] == "周杰倫"
    finally:
        _teardown_logger(handler)


def test_log_song_start_collapses_newlines(tmp_path):
    log_path = tmp_path / "x.log"
    handler = configure_marvin_speech_logger(str(log_path))
    try:
        log_song_start("第一行\n第二行", start_ts=1.0)
        handler.flush()
        row = _last_json_line(log_path)
        assert row["text"] == "第一行 第二行"
    finally:
        _teardown_logger(handler)


def test_log_song_start_skips_blank_title(tmp_path):
    log_path = tmp_path / "x.log"
    handler = configure_marvin_speech_logger(str(log_path))
    try:
        log_song_start("  \n ", start_ts=1.0)
        handler.flush()
        assert not log_path.exists() or log_path.read_text(encoding="utf-8").strip() == ""
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

    with patch("os.path.exists", return_value=True):
        await cog._maybe_play_dj_interjection(dj)

    vc.play_dj_on_tts_layer.assert_awaited_once()
    _, kwargs = vc.play_dj_on_tts_layer.call_args
    assert kwargs["text"] == "這首歌是狗與露點的"


# ── 10. _speak_song_ack 把「幫你點了《X》」原稿傳進 play_dj_on_tts_layer ─────────

@pytest.mark.asyncio
async def test_speak_song_ack_passes_ack_text_through():
    cog, vc = _make_music_cog_for_interjection()
    cog.bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/ack.mp3")

    await cog._speak_song_ack(vc, {"title": "七里香"})

    cog.bot.tts_engine.generate_audio.assert_awaited_once_with("幫你點了《七里香》")
    vc.play_dj_on_tts_layer.assert_awaited_once()
    _, kwargs = vc.play_dj_on_tts_layer.call_args
    assert kwargs["text"] == "幫你點了《七里香》"


@pytest.mark.asyncio
async def test_speak_song_ack_uses_canon_title():
    """曲庫正規化過就唸正規化曲名，不唸 YouTube 原標題。"""
    cog, vc = _make_music_cog_for_interjection()
    cog.bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/ack.mp3")

    await cog._speak_song_ack(vc, {
        "title": "周杰倫 Jay Chou【雙截棍 Nunchucks】Official MV",
        "_canon": {"title": "雙截棍", "artist": "周杰倫"},
    })

    cog.bot.tts_engine.generate_audio.assert_awaited_once_with("幫你點了《雙截棍》")


@pytest.mark.asyncio
async def test_speak_song_ack_reads_cached_canon():
    """點歌當下 info 還沒掛 _canon（要等 DJ 預產才掛），播過的歌要從曲庫快取補上。"""
    cog, vc = _make_music_cog_for_interjection()
    cog.bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/ack.mp3")
    store = MagicMock()
    store.get = MagicMock(side_effect=lambda k: {"title": "如果我很平庸", "artist": "陳華"}
                          if k == "canon::dQw4w9WgXcQ" else None)
    cog._audiophile_deps = MagicMock(return_value=(store, None, None))

    await cog._speak_song_ack(vc, {
        "title": "陳華Hua Chen【如果我很平庸Perfectly Ordinary】Official MV",
        "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    })

    cog.bot.tts_engine.generate_audio.assert_awaited_once_with("幫你點了《如果我很平庸》")


@pytest.mark.asyncio
async def test_speak_song_ack_strips_youtube_cruft():
    """沒正規化資料時走 DJ 乾淨歌名，至少剝掉 Official MV 這類 YouTube 雜訊。"""
    cog, vc = _make_music_cog_for_interjection()
    cog.bot.tts_engine.generate_audio = AsyncMock(return_value="/tmp/ack.mp3")

    await cog._speak_song_ack(vc, {"title": "陳華Hua Chen【如果我很平庸Perfectly Ordinary】Official MV"})

    (ack_text,), _ = cog.bot.tts_engine.generate_audio.call_args
    assert "Official" not in ack_text and "MV" not in ack_text
    assert "如果我很平庸" in ack_text


# ── 11. ack_templates.text_for_file ─────────────────────────────────────────

def test_text_for_file_finds_by_basename():
    import ack_templates

    assert ack_templates.text_for_file("music_ack_03.mp3") == "這首好聽"
    assert ack_templates.text_for_file("/any/dir/assets/acks/music/music_ack_03.mp3") == "這首好聽"
    assert ack_templates.text_for_file("nope.mp3") is None


# ── 12. log_marvin_speech 的 file kwarg ──────────────────────────────────────

def test_log_marvin_speech_file_kwarg_present_and_absent(tmp_path):
    log_path = tmp_path / "x.log"
    handler = configure_marvin_speech_logger(str(log_path))
    try:
        log_marvin_speech("有檔案", start_ts=1.0, layer=1, voice=None, src="ack", file="a/b.mp3")
        handler.flush()
        row = _last_json_line(log_path)
        assert row["file"] == "a/b.mp3"

        log_marvin_speech("沒檔案", start_ts=1.0, layer=1, voice=None, src="tts")
        handler.flush()
        row2 = _last_json_line(log_path)
        assert "file" not in row2
    finally:
        _teardown_logger(handler)


# ── 13-15. _play_ack 寫 marvin_speech.log ───────────────────────────────────

@pytest.mark.asyncio
async def test_play_ack_logs_with_text_and_file(tmp_path):
    from tests.test_tts_uniform_loudness import _make_ack_cog, _idle_vc

    cog = _make_ack_cog()
    vc = _idle_vc()
    cog.voice_client = vc
    cog._mixer.push_tts.return_value = True

    f = tmp_path / "music_ack_03.mp3"
    f.write_bytes(b"x")

    with patch("glob.glob", return_value=[str(f)]), \
         patch("cogs.voice_controller_playback.log_marvin_speech") as mock_log:
        await cog._play_ack("music", speaker="阿狗")

    mock_log.assert_called_once()
    args, kwargs = mock_log.call_args
    assert args[0] == "這首好聽"
    assert kwargs["src"] == "ack"
    assert kwargs["file"] == str(f)
    assert isinstance(kwargs["start_ts"], float)


@pytest.mark.asyncio
async def test_play_ack_push_fails_does_not_log(tmp_path):
    from tests.test_tts_uniform_loudness import _make_ack_cog, _idle_vc

    cog = _make_ack_cog()
    vc = _idle_vc()
    cog.voice_client = vc
    cog._mixer.push_tts.return_value = False

    f = tmp_path / "music_ack_03.mp3"
    f.write_bytes(b"x")

    with patch("glob.glob", return_value=[str(f)]), \
         patch("cogs.voice_controller_playback.log_marvin_speech") as mock_log:
        await cog._play_ack("music", speaker="阿狗")

    mock_log.assert_not_called()


@pytest.mark.asyncio
async def test_play_ack_unknown_file_logs_bracketed_basename(tmp_path):
    from tests.test_tts_uniform_loudness import _make_ack_cog, _idle_vc

    cog = _make_ack_cog()
    vc = _idle_vc()
    cog.voice_client = vc
    cog._mixer.push_tts.return_value = True

    f = tmp_path / "zzz.mp3"
    f.write_bytes(b"x")

    with patch("glob.glob", return_value=[str(f)]), \
         patch("cogs.voice_controller_playback.log_marvin_speech") as mock_log:
        await cog._play_ack("music", speaker="阿狗")

    mock_log.assert_called_once()
    args, _ = mock_log.call_args
    assert args[0] == "[zzz.mp3]"


# ── origin：Discord bot / satellite / local 三個程序共寫同一份 log，要標出是誰講的 ──

@pytest.fixture
def _reset_origin():
    import marvin_speech_log
    yield
    marvin_speech_log.set_origin("discord")


def test_origin_defaults_to_discord(tmp_path, _reset_origin):
    log_path = tmp_path / "x.log"
    handler = configure_marvin_speech_logger(str(log_path))
    try:
        log_marvin_speech("嗨", start_ts=1790000000.0, layer=1, voice=None, src="tts")
        assert _last_json_line(log_path)["origin"] == "discord"
    finally:
        _teardown_logger(handler)


def test_set_origin_applies_to_speech_and_song(tmp_path, _reset_origin):
    import marvin_speech_log

    log_path = tmp_path / "x.log"
    handler = configure_marvin_speech_logger(str(log_path))
    try:
        marvin_speech_log.set_origin("satellite")
        assert marvin_speech_log.get_origin() == "satellite"
        log_marvin_speech("順便報一下新聞", start_ts=1790000000.0, layer=1, voice=None, src="tts")
        assert _last_json_line(log_path)["origin"] == "satellite"
        log_song_start("七里香", start_ts=1790000001.0)
        assert _last_json_line(log_path)["origin"] == "satellite"
    finally:
        _teardown_logger(handler)


@pytest.mark.parametrize("module_name, expected", [("main_local", "local")])
def test_entrypoint_sets_origin_before_building_bot(monkeypatch, _reset_origin, module_name, expected):
    import importlib
    import main_discord
    import marvin_speech_log

    seen = {}

    class _FakeBot:
        def __init__(self):
            seen["origin"] = marvin_speech_log.get_origin()

    monkeypatch.setattr(main_discord, "MarvinBot", _FakeBot)
    mod = importlib.import_module(module_name)
    mod.build_local_bot()
    assert seen["origin"] == expected
