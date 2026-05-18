"""
TDD：Issue 7 — Personal Assistant fast-track 在 _recall_handler 未啟用時
應該 log warning，而不是 silent skip。

問題：原本 `if self._recall_handler and is_manual_add_query(query):` 把
handler 存在性放在 intent 偵測前，handler=None 時連 intent 都不會檢查，
意圖完全消失。使用者「記一下要買牛奶」會被當閒聊送進 Marvin LLM，
沒人知道 PA 功能其實沒啟用。

修法：先驗 intent，後驗 handler；handler 缺失時記 warning 讓 debug 可見。
"""
from __future__ import annotations

import logging
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    bot.voice_clients = []
    bot.cogs.get.return_value = None
    bot.tts_engine = MagicMock()
    bot.tts_engine.get_estimated_duration.return_value = 2.0
    bot.router = MagicMock()
    bot.router._background_intent_enrich = AsyncMock()
    bot.router.classify_query_route = AsyncMock(return_value="marvin")
    bot.router.memory = MagicMock()
    bot.router.memory.list_players = MagicMock(return_value=[])
    bot.router.VISION_KEYWORDS = ()
    bot.vision_enabled = False
    bot.visual_buffer = None
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])

    with patch("discord.ext.tasks.loop", lambda *a, **kw: lambda f: f), \
         patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        cog = VoiceController(bot)

    cog.active_text_channel = AsyncMock()
    placeholder_msg = MagicMock()
    placeholder_msg.edit = AsyncMock()
    placeholder_msg.delete = AsyncMock()
    cog.active_text_channel.send = AsyncMock(return_value=placeholder_msg)
    cog.log_buffer = []
    cog.stt_logger = MagicMock()
    cog.stream_queue = []
    cog.stream_history = []
    cog.stream_mode = False
    cog.radio_mode = False
    cog.is_playing_audio = False
    cog.tts_queue_duration = 0.0
    cog._tts_protected = False
    cog._tts_interrupted = False
    cog._awaiting_confirmation = False
    cog._awaiting_confirmation_speaker = None
    cog.user_emotion_cache = {}
    cog.marvin_self_emotion = {}
    cog.speech_buffers = {}
    cog._wake_response_pending = False
    cog._is_owner_speaker = MagicMock(return_value=False)
    cog._query_quality_gate = MagicMock(return_value=(True, "ok"))

    async def _empty_stream():
        if False:
            yield ""
    cog.bot.router.stream_fast_response = MagicMock(return_value=_empty_stream())

    async def _empty_sentence_gen(stream):
        if False:
            yield ""
    cog._stream_sentence_splitter = _empty_sentence_gen
    cog._cot_filter_stream = lambda s: s

    return cog


def _set_query(cog, query: str):
    cog.bot.engine.conv_buffer.get_harvest = MagicMock(return_value=query)


# ── PA intent 命中但 handler=None → 必須 warn ─────────────────────────────

@pytest.mark.asyncio
async def test_manual_add_warns_when_recall_handler_none(caplog):
    cog = _make_cog()
    cog._recall_handler = None
    _set_query(cog, "記一下要買牛奶")  # 命中 _MANUAL_ADD_PATTERNS

    with caplog.at_level(logging.WARNING, logger="cogs.voice_controller"):
        await cog._process_queued_query("Alice", wake_time=100.0)

    pa_warnings = [r for r in caplog.records if "PA Disabled" in r.message]
    assert pa_warnings, "manual_add intent 應該觸發 PA Disabled warning"
    assert "manual_add" in pa_warnings[0].message
    assert "記一下要買牛奶" in pa_warnings[0].message


@pytest.mark.asyncio
async def test_mark_done_warns_when_recall_handler_none(caplog):
    cog = _make_cog()
    cog._recall_handler = None
    _set_query(cog, "那件事做完了")  # 命中 _MARK_DONE_PATTERNS

    with caplog.at_level(logging.WARNING, logger="cogs.voice_controller"):
        await cog._process_queued_query("Alice", wake_time=100.0)

    pa_warnings = [r for r in caplog.records if "PA Disabled" in r.message]
    assert pa_warnings, "mark_done intent 應該觸發 PA Disabled warning"
    assert "mark_done" in pa_warnings[0].message


@pytest.mark.asyncio
async def test_recall_warns_when_recall_handler_none(caplog):
    cog = _make_cog()
    cog._recall_handler = None
    _set_query(cog, "我剛才說了什麼")  # 命中 _RECALL_PATTERNS

    with caplog.at_level(logging.WARNING, logger="cogs.voice_controller"):
        await cog._process_queued_query("Alice", wake_time=100.0)

    pa_warnings = [r for r in caplog.records if "PA Disabled" in r.message]
    assert pa_warnings, "recall intent 應該觸發 PA Disabled warning"


# ── handler 存在時：照常呼叫 handler，不 warn ─────────────────────────────

@pytest.mark.asyncio
async def test_no_warning_when_recall_handler_set(caplog):
    cog = _make_cog()
    cog._recall_handler = MagicMock()  # 模擬已啟用
    cog._handle_manual_add_query = AsyncMock()
    _set_query(cog, "記一下要買牛奶")

    with caplog.at_level(logging.WARNING, logger="cogs.voice_controller"):
        await cog._process_queued_query("Alice", wake_time=100.0)

    pa_warnings = [r for r in caplog.records if "PA Disabled" in r.message]
    assert not pa_warnings, "handler 存在時不該 warn"
    cog._handle_manual_add_query.assert_awaited_once()


# ── 非 PA query 不該 warn ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_warning_when_query_is_not_pa_intent(caplog):
    cog = _make_cog()
    cog._recall_handler = None
    _set_query(cog, "今天天氣怎麼樣")  # 完全不是 PA query

    with caplog.at_level(logging.WARNING, logger="cogs.voice_controller"):
        await cog._process_queued_query("Alice", wake_time=100.0)

    pa_warnings = [r for r in caplog.records if "PA Disabled" in r.message]
    assert not pa_warnings, "非 PA query 不該 warn"
