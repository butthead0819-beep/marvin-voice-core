import pytest
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from gemini_router_llm import WebSearchError
except ImportError:
    class WebSearchError(RuntimeError):
        pass

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
    bot.router.memory.get_player_data.return_value = {}
    bot.router.memory.list_players = MagicMock(return_value=[])
    bot.router.VISION_KEYWORDS = ("看畫面", "看一下螢幕")
    bot.vision_enabled = True
    bot.visual_buffer = MagicMock()
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
    cog._recall_handler = None
    cog.user_emotion_cache = {}
    cog.marvin_self_emotion = {}
    cog.speech_buffers = {}
    cog._wake_response_pending = False
    
    cog._ducking_agent = MagicMock()
    cog._ducking_agent.wake_threshold_boost.return_value = 0.0

    # Mock side effect paths to avoid triggers
    cog._handle_nemoclaw_query = AsyncMock()
    cog._handle_marmo_query = AsyncMock()
    cog._safe_music_command = AsyncMock()
    cog._handle_voice_music_command = AsyncMock()
    cog._handle_voice_imitate_command = AsyncMock()
    cog._handle_voice_status_query = AsyncMock()
    cog._process_vision_query = AsyncMock()
    cog._handle_recall_query = AsyncMock()
    cog._is_owner_speaker = MagicMock(return_value=True)
    cog._query_quality_gate = MagicMock(return_value=(True, "ok"))
    
    cog._intent_bus = AsyncMock()
    cog._intent_bus.dispatch = AsyncMock(return_value=None)
    
    # Mock play_tts
    cog.play_tts = AsyncMock()

    cog._cot_filter_stream = lambda s: s

    return cog

def _set_query(cog, query: str):
    cog.bot.engine.conv_buffer.get_harvest = MagicMock(return_value=query)

# ── 測試 1：網頁搜尋失敗 (WebSearchError) ──
@pytest.mark.asyncio
async def test_fallback_web_search_error():
    cog = _make_cog()
    _set_query(cog, "馬文，幫我查今天天氣")

    async def _error_stream():
        yield "__SEARCHING__"
        raise WebSearchError("DuckDuckGo search failed")

    cog.bot.router.stream_fast_response = MagicMock(return_value=_error_stream())
    
    await cog._process_queued_query("Alice", wake_time=time.time(), wake_intent=0.95)
    
    cog.play_tts.assert_awaited_once_with(
        "我試著上網幫你搜尋資料，但搜尋引擎暫時沒有回應。",
        already_in_channel=True,
        emotion_tag="neutral"
    )

# ── 測試 2：配額用盡 (QuotaExhaustedError / 429) ──
@pytest.mark.asyncio
async def test_fallback_quota_exhausted_error():
    cog = _make_cog()
    _set_query(cog, "你好嗎")

    async def _error_stream():
        if False:
            yield ""
        raise RuntimeError("API quota exceeded (ResourceExhausted 429)")

    cog.bot.router.stream_fast_response = MagicMock(return_value=_error_stream())
    
    await cog._process_queued_query("Alice", wake_time=time.time(), wake_intent=0.95)
    
    cog.play_tts.assert_awaited_once_with(
        "我的 API 配額似乎已經用完了，無法建立思緒連結。",
        already_in_channel=True,
        emotion_tag="neutral"
    )

# ── 測試 3：網路超時 (TimeoutError) ──
@pytest.mark.asyncio
async def test_fallback_timeout_error():
    cog = _make_cog()
    _set_query(cog, "你好嗎")

    async def _error_stream():
        if False:
            yield ""
        raise asyncio.TimeoutError("Connection timed out")

    cog.bot.router.stream_fast_response = MagicMock(return_value=_error_stream())
    
    await cog._process_queued_query("Alice", wake_time=time.time(), wake_intent=0.95)
    
    cog.play_tts.assert_awaited_once_with(
        "我的大腦連結伺服器超時了，請確認網路連線是否正常。",
        already_in_channel=True,
        emotion_tag="neutral"
    )

# ── 測試 4：一般錯誤 (General Error) ──
@pytest.mark.asyncio
async def test_fallback_general_error():
    cog = _make_cog()
    _set_query(cog, "你好嗎")

    async def _error_stream():
        if False:
            yield ""
        raise ValueError("Some general unexpected error")

    cog.bot.router.stream_fast_response = MagicMock(return_value=_error_stream())
    
    await cog._process_queued_query("Alice", wake_time=time.time(), wake_intent=0.95)
    
    cog.play_tts.assert_awaited_once_with(
        "大腦思緒在連結中斷了，請再說一次。",
        already_in_channel=True,
        emotion_tag="neutral"
    )

# ── 測試 5：低信心 (tts_suppressed) 不播報 TTS ──
@pytest.mark.asyncio
async def test_fallback_suppressed_on_low_confidence():
    cog = _make_cog()
    _set_query(cog, "你好嗎")

    async def _error_stream():
        if False:
            yield ""
        raise ValueError("Some error")

    cog.bot.router.stream_fast_response = MagicMock(return_value=_error_stream())
    
    # wake_intent = 0.5 < 0.80 -> tts_suppressed = True
    await cog._process_queued_query("Alice", wake_time=time.time(), wake_intent=0.5)
    
    cog.play_tts.assert_not_awaited()

# ── 測試 6：首句已收到，後續 Exception 不播報 TTS ──
@pytest.mark.asyncio
async def test_fallback_not_triggered_after_first_sentence():
    cog = _make_cog()
    _set_query(cog, "你好嗎")

    async def _error_stream():
        yield "今天天氣很好。"
        raise ValueError("Some connection break after first sentence")

    cog.bot.router.stream_fast_response = MagicMock(return_value=_error_stream())
    
    await cog._process_queued_query("Alice", wake_time=time.time(), wake_intent=0.95)
    await asyncio.sleep(0.1)
    
    # 應該只播放了第一句，不播放 fallback 語音
    cog.play_tts.assert_awaited_once_with(
        "今天天氣很好。",
        already_in_channel=True,
        emotion_tag="neutral"
    )
