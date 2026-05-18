"""
TDD：Issue 2 — wake_intent 必須 gate 副作用 fast-track。

問題：_process_queued_query 的 wake_intent < 0.80 目前只 gate TTS 輸出，
不 gate 任何 fast-track 路由。低信心喚醒（可能是背景對話被誤判）仍會
執行 NemoClaw / 音樂播放 / 視覺截圖等副作用，造成誤觸發。

意圖層面的修法：
- wake_intent < 0.80 → 跳過所有副作用 fast-track（NemoClaw / Marmo /
  PA 寫入 / Vision / Music / Imitation）
- 仍允許資訊類路徑：quality gate / status / recall / LLM 純文字回應
- Track A regex (wake_intent=None) 視為高信心，照常跑（既有行為）
- Track B wake_intent >= 0.80 也照常跑
"""
from __future__ import annotations

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
    cog._recall_handler = None  # 預設不啟用 PA 路徑，避免污染測試
    cog.user_emotion_cache = {}
    cog.marvin_self_emotion = {}
    cog.speech_buffers = {}
    cog._wake_response_pending = False

    # 把所有副作用 fast-track handler mock 起來，以便 assert 是否被呼叫
    cog._handle_nemoclaw_query = AsyncMock()
    cog._handle_marmo_query = AsyncMock()
    cog._handle_voice_music_command = AsyncMock()
    cog._handle_voice_imitate_command = AsyncMock()
    cog._handle_voice_status_query = AsyncMock()
    cog._process_vision_query = AsyncMock()
    cog._handle_recall_query = AsyncMock()

    # owner 判定預設 True，讓 NemoClaw smart router 路徑能進入
    cog._is_owner_speaker = MagicMock(return_value=True)

    # query quality gate 預設通過
    cog._query_quality_gate = MagicMock(return_value=(True, "ok"))

    # LLM 路徑：給一個空 async generator，避免真的 stream
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


# ── 副作用 fast-track 應被 wake_intent < 0.80 gate ─────────────────────────

@pytest.mark.asyncio
async def test_low_confidence_skips_music_fast_track():
    cog = _make_cog()
    _set_query(cog, "播放陶喆的天天")  # 強訊號 music play

    await cog._process_queued_query("Alice", wake_time=100.0, wake_intent=0.5)

    cog._handle_voice_music_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_low_confidence_skips_nemoclaw_regex_fast_track():
    cog = _make_cog()
    _set_query(cog, "查一下今天天氣")
    # original_raw 含「龍蝦」喚醒詞 → 正常應命中 NemoClaw regex fast-track
    await cog._process_queued_query(
        "Alice", wake_time=100.0,
        wake_intent=0.5,
        original_raw="龍蝦，查一下今天天氣",
    )

    cog._handle_nemoclaw_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_low_confidence_skips_vision_fast_track():
    cog = _make_cog()
    _set_query(cog, "看畫面這是什麼")  # 命中 VISION_KEYWORDS

    await cog._process_queued_query("Alice", wake_time=100.0, wake_intent=0.5)

    cog._process_vision_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_low_confidence_skips_nemoclaw_smart_router():
    cog = _make_cog()
    _set_query(cog, "幫我查一下今天天氣")
    cog.bot.router.classify_query_route = AsyncMock(return_value="nemoclaw")

    await cog._process_queued_query("Alice", wake_time=100.0, wake_intent=0.5)

    cog._handle_nemoclaw_query.assert_not_awaited()


# ── 既有 high-confidence 行為要保留 ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_high_confidence_runs_music_fast_track():
    """wake_intent=0.95 (高信心) → music fast-track 應照常執行。"""
    cog = _make_cog()
    _set_query(cog, "播放陶喆的天天")

    await cog._process_queued_query("Alice", wake_time=100.0, wake_intent=0.95)

    cog._handle_voice_music_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_track_a_none_intent_runs_music_fast_track():
    """wake_intent=None (Track A regex 高信心預設) → 照常跑 fast-track。"""
    cog = _make_cog()
    _set_query(cog, "播放陶喆的天天")

    await cog._process_queued_query("Alice", wake_time=100.0, wake_intent=None)

    cog._handle_voice_music_command.assert_awaited_once()


# ── 資訊類（讀取）路徑在低信心時仍允許 ─────────────────────────────────────

@pytest.mark.asyncio
async def test_low_confidence_allows_status_query():
    """系統狀態查詢是 READ-ONLY → 即使低信心也應該回答。"""
    cog = _make_cog()
    _set_query(cog, "馬文系統狀態如何")

    await cog._process_queued_query("Alice", wake_time=100.0, wake_intent=0.5)

    cog._handle_voice_status_query.assert_awaited_once()
