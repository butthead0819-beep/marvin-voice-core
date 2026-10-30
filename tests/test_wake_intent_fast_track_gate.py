"""
wake_intent 與副作用 fast-track。

原本測 wake_intent < 0.80 跳過副作用 fast-track 的低信心喚醒閘；意圖漏斗步驟 2
（2026-10-26）因 wake_intent 在 handle_stt_result 恆為 None 而整條刪除，只留
「照常執行」與資訊類路徑的案例。
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
    bot.router.memory = MagicMock()
    bot.router.memory.get_player_data.return_value = {}
    bot.router.memory.list_players = MagicMock(return_value=[])
    bot.router.VISION_KEYWORDS = ("看畫面", "看一下螢幕")
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
    cog._safe_music_command = AsyncMock()
    cog._handle_voice_music_command = AsyncMock()
    cog._process_vision_query = AsyncMock()
    cog._handle_recall_query = AsyncMock()

    # owner 判定預設 True
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


# 意圖漏斗步驟 2 刪除：test_low_confidence_skips_{music,nemoclaw_regex,vision,nemoclaw_smart_router}
#（wake_intent 自 2026-06-10 起在 handle_stt_result 恆為 None，低信心喚醒閘整條已拔）。

# ── 既有 high-confidence 行為要保留 ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_high_confidence_runs_music_fast_track():
    """wake_intent=0.95 (高信心) → music fast-track 應照常執行。"""
    cog = _make_cog()
    _set_query(cog, "播放陶喆的天天")

    await cog._process_queued_query("Alice", wake_time=100.0, wake_intent=0.95)

    cog._safe_music_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_track_a_none_intent_runs_music_fast_track():
    """wake_intent=None (Track A regex 高信心預設) → 照常跑 fast-track。"""
    cog = _make_cog()
    _set_query(cog, "播放陶喆的天天")

    await cog._process_queued_query("Alice", wake_time=100.0, wake_intent=None)

    cog._safe_music_command.assert_awaited_once()


# ── 資訊類（讀取）路徑在低信心時仍允許 ─────────────────────────────────────

@pytest.mark.asyncio
async def test_low_confidence_allows_status_query():
    """系統狀態查詢是 READ-ONLY → 即使低信心也應該回答。"""
    cog = _make_cog()
    _set_query(cog, "馬文系統狀態如何")

    await cog._process_queued_query("Alice", wake_time=100.0, wake_intent=0.5)

    cog._handle_voice_status_query.assert_awaited_once()
