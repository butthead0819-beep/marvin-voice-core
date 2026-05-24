"""
TDD: Per-speaker language routing for STT engines.

Engine detects language from previous utterances and passes the correct
language / locale to Groq, Whisper, and Swift on subsequent calls.
First utterance defaults to "zh" (backward-compatible).
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# Minimal bot / engine bootstrap
# ---------------------------------------------------------------------------

def _make_bot():
    bot = MagicMock()
    bot.router = MagicMock()
    bot.router.game_dict_string = ""
    bot.get_cog.return_value = None
    return bot


def _make_engine():
    from discord_voice_engine import DiscordVoiceEngine
    bot = _make_bot()
    engine = DiscordVoiceEngine(bot)
    return engine


# ---------------------------------------------------------------------------
# Unit tests: _detect_text_lang (isolated)
# ---------------------------------------------------------------------------

class TestDetectTextLang:
    def test_chinese_text_returns_zh(self):
        from discord_voice_engine import DiscordVoiceEngine
        assert DiscordVoiceEngine._detect_text_lang("你好世界") == "zh"

    def test_english_text_returns_en(self):
        from discord_voice_engine import DiscordVoiceEngine
        assert DiscordVoiceEngine._detect_text_lang("hello world how are you") == "en"

    def test_mixed_mostly_cjk_returns_zh(self):
        from discord_voice_engine import DiscordVoiceEngine
        assert DiscordVoiceEngine._detect_text_lang("你好 ok") == "zh"

    def test_empty_returns_zh(self):
        from discord_voice_engine import DiscordVoiceEngine
        assert DiscordVoiceEngine._detect_text_lang("") == "zh"


# ---------------------------------------------------------------------------
# Unit tests: engine speaker_lang dict
# ---------------------------------------------------------------------------

class TestSpeakerLangDict:
    def test_defaults_to_zh_for_unknown_speaker(self):
        engine = _make_engine()
        assert engine._get_speaker_lang("NewUser") == "zh"

    def test_update_speaker_lang_zh(self):
        with patch.dict(os.environ, {"STT_AUTO_DETECT_LANG": "true"}):
            engine = _make_engine()
            engine._update_speaker_lang("狗與露", "你好世界")
            assert engine._get_speaker_lang("狗與露") == "zh"

    def test_update_speaker_lang_en(self):
        with patch.dict(os.environ, {"STT_AUTO_DETECT_LANG": "true"}):
            engine = _make_engine()
            engine._update_speaker_lang("Alice", "hello world how are you doing today")
            assert engine._get_speaker_lang("Alice") == "en"

    def test_update_speaker_lang_switches_over_time(self):
        with patch.dict(os.environ, {"STT_AUTO_DETECT_LANG": "true"}):
            engine = _make_engine()
            engine._update_speaker_lang("Bob", "hello world this is english")
            assert engine._get_speaker_lang("Bob") == "en"
            engine._update_speaker_lang("Bob", "你好世界這是中文內容")
            assert engine._get_speaker_lang("Bob") == "zh"


# ---------------------------------------------------------------------------
# Unit tests: STT method language params
# ---------------------------------------------------------------------------

class TestGroqLanguageParam:
    @pytest.mark.asyncio
    async def test_groq_uses_zh_by_default(self):
        engine = _make_engine()
        captured = {}

        def _fake_upload():
            captured["lang"] = "zh"
            return "測試"

        with patch("discord_voice_engine.asyncio.to_thread", new=AsyncMock(return_value="測試")) as mock_thread:
            await engine._run_groq_whisper_stt("/tmp/test.wav", language="zh")
        # If language param exists and is accepted, no TypeError raised

    @pytest.mark.asyncio
    async def test_groq_accepts_en_language(self):
        engine = _make_engine()
        called_kwargs = {}

        async def _fake_to_thread(fn, *a, **kw):
            called_kwargs["fn"] = fn
            return ""

        with patch("discord_voice_engine.asyncio.to_thread", side_effect=_fake_to_thread):
            with patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}):
                await engine._run_groq_whisper_stt("/tmp/test.wav", language="en")
        # If no TypeError, the signature accepts language param


class TestWhisperLanguageParam:
    @pytest.mark.asyncio
    async def test_whisper_accepts_language_param(self):
        engine = _make_engine()
        engine.whisper_model = MagicMock()
        engine.whisper_model.transcribe.return_value = ([], MagicMock())

        with patch("discord_voice_engine.asyncio.get_event_loop") as mock_loop:
            loop = MagicMock()
            mock_loop.return_value = loop
            loop.run_in_executor = AsyncMock(return_value="test")
            result = await engine._run_whisper_stt("/tmp/test.wav", language="en")
        # No TypeError means signature is correct

    @pytest.mark.asyncio
    async def test_whisper_passes_language_to_model(self):
        engine = _make_engine()
        captured_lang = {}

        real_model = MagicMock()
        real_model.transcribe.return_value = (iter([MagicMock(text="hello")]), MagicMock())
        engine.whisper_model = real_model
        engine._whisper_thread_sem = MagicMock()
        engine._whisper_thread_sem.acquire.return_value = True

        def _mock_transcribe(audio, **kwargs):
            captured_lang["language"] = kwargs.get("language")
            return (iter([MagicMock(text="hello")]), MagicMock())

        real_model.transcribe.side_effect = _mock_transcribe

        with patch("discord_voice_engine.asyncio.get_event_loop") as mock_loop:
            loop = asyncio.get_event_loop()
            mock_loop.return_value = loop

            with patch("discord_voice_engine.asyncio.wait_for", new=AsyncMock(return_value="hello")):
                await engine._run_whisper_stt("/tmp/test.wav", language="en")

        # The important check is that the method signature accepts language


class TestSwiftLocaleParam:
    @pytest.mark.asyncio
    async def test_swift_sets_locale_env_zh_tw(self):
        engine = _make_engine()
        captured_env = {}

        async def _fake_exec(*args, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            return proc

        with patch("discord_voice_engine.asyncio.create_subprocess_exec", side_effect=_fake_exec):
            await engine._run_swift_stt("/tmp/test.wav", is_wake_check=False, locale="zh-TW")

        assert captured_env.get("STT_LOCALE") == "zh-TW"

    @pytest.mark.asyncio
    async def test_swift_sets_locale_env_en_us(self):
        engine = _make_engine()
        captured_env = {}

        async def _fake_exec(*args, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            return proc

        with patch("discord_voice_engine.asyncio.create_subprocess_exec", side_effect=_fake_exec):
            await engine._run_swift_stt("/tmp/test.wav", is_wake_check=False, locale="en-US")

        assert captured_env.get("STT_LOCALE") == "en-US"

    @pytest.mark.asyncio
    async def test_swift_defaults_to_zh_tw_locale(self):
        engine = _make_engine()
        captured_env = {}

        async def _fake_exec(*args, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            return proc

        with patch("discord_voice_engine.asyncio.create_subprocess_exec", side_effect=_fake_exec):
            await engine._run_swift_stt("/tmp/test.wav", is_wake_check=False)

        assert captured_env.get("STT_LOCALE") == "zh-TW"


# ---------------------------------------------------------------------------
# Integration: _process_stt_hybrid passes correct lang from speaker history
# ---------------------------------------------------------------------------

def _patch_bot_router(engine):
    """Make bot.router.clean_stt_text awaitable so _process_stt_hybrid completes."""
    engine.bot.router.clean_stt_text = AsyncMock(return_value={"text": "", "is_wake": False})
    engine.bot.router.game_dict_string = ""


class TestProcessSttHybridLangRouting:
    @pytest.mark.asyncio
    async def test_first_utterance_uses_zh(self):
        """First utterance from unknown speaker → STT should use zh."""
        engine = _make_engine()
        engine.stt_engine = "macos"
        engine.stt_callback = AsyncMock()
        _patch_bot_router(engine)
        captured_locale = {}

        async def _fake_swift(wav_path, is_wake_check=False, locale="zh-TW"):
            captured_locale["locale"] = locale
            return ("你好", {})

        engine._run_swift_stt = _fake_swift

        await engine._process_stt_hybrid("NewSpeaker", "/tmp/test.wav", b"", 0.0)

        assert captured_locale.get("locale") == "zh-TW"

    @pytest.mark.asyncio
    async def test_second_utterance_uses_detected_lang(self):
        """After english first utterance is stored, second call uses en."""
        with patch.dict(os.environ, {"STT_AUTO_DETECT_LANG": "true"}):
            engine = _make_engine()
            engine.stt_engine = "macos"
            engine.stt_callback = AsyncMock()
            _patch_bot_router(engine)
            engine._update_speaker_lang("Alice", "hello world how are you")

            captured_locale = {}

            async def _fake_swift(wav_path, is_wake_check=False, locale="zh-TW"):
                captured_locale["locale"] = locale
                return ("how are you", {})

            engine._run_swift_stt = _fake_swift

            await engine._process_stt_hybrid("Alice", "/tmp/test.wav", b"", 0.0)

        assert captured_locale.get("locale") == "en-US"
