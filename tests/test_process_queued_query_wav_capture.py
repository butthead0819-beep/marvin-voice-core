"""Audio Rescue v2：_process_queued_query() 擷取 wav_bytes 進 IntentContext。

speech_buffers[speaker]["wav_bytes"] 在 query 擷取階段就會被 pop 丟棄；這裡驗證
pop 前先把它存進 _bus_ctx.audio_wav_bytes，供後面 IntentBus rescue 用。
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


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
    bot.engine.conv_buffer.get_harvest = MagicMock(return_value="")

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

    cog.play_tts = AsyncMock()
    cog._cot_filter_stream = lambda s: s

    return cog


@pytest.mark.asyncio
async def test_harvest_hit_still_captures_wav_bytes_from_speech_buffers():
    """harvest 命中（有文字）時，speech_buffers 仍照樣 pop，wav_bytes 要一併帶走。"""
    cog = _make_cog()
    cog.bot.engine.conv_buffer.get_harvest = MagicMock(return_value="小聲一點")
    cog.speech_buffers["Alice"] = {"texts": ["小聲一點"], "wav_bytes": bytearray(b"raw-audio")}

    await cog._process_queued_query("Alice", wake_time=time.time(), wake_intent=0.95)

    assert "Alice" not in cog.speech_buffers  # 照舊被 pop 丟棄
    ctx = cog._intent_bus.dispatch.await_args.args[0]
    assert ctx.audio_wav_bytes == b"raw-audio"


@pytest.mark.asyncio
async def test_harvest_empty_fallback_still_captures_wav_bytes():
    """harvest 為空、fallback 到 speech_buffers 文字時，同樣要擷取 wav_bytes。"""
    cog = _make_cog()
    cog.bot.engine.conv_buffer.get_harvest = MagicMock(return_value="")
    cog.speech_buffers["Alice"] = {"texts": ["小聲一點"], "wav_bytes": bytearray(b"raw-audio-2")}

    await cog._process_queued_query("Alice", wake_time=time.time(), wake_intent=0.95)

    ctx = cog._intent_bus.dispatch.await_args.args[0]
    assert ctx.audio_wav_bytes == b"raw-audio-2"


@pytest.mark.asyncio
async def test_override_query_path_captures_wav_bytes_from_speech_buffers():
    """override_query 是實際唯一路徑（_confirmation_flow → _process_queued_query）。
    喚醒句就是完整指令時，speech_buffers 裡的音訊正是這輪要救援的音訊，必須帶走。"""
    cog = _make_cog()
    cog.speech_buffers["Alice"] = {"texts": ["馬文小聲一點"], "wav_bytes": bytearray(b"wake-audio")}

    await cog._process_queued_query(
        "Alice", wake_time=time.time(), wake_intent=0.95, override_query="小聲一點",
    )

    assert "Alice" not in cog.speech_buffers  # 照舊被 pop 丟棄
    ctx = cog._intent_bus.dispatch.await_args.args[0]
    assert ctx.audio_wav_bytes == b"wake-audio"


@pytest.mark.asyncio
async def test_override_query_path_falls_back_to_prev_turn_audio_snapshot():
    """等問句情境下 speech_buffers 可能已被 follow-up 的 debounce pop 掉；
    退回 _prev_turn_audio 快照，讓 Audio Rescue / Frustration 仍有音訊可用。"""
    cog = _make_cog()
    cog._prev_turn_audio = {"Alice": b"snapshot-audio"}
    # speech_buffers 沒有 Alice（已被 pop）

    await cog._process_queued_query(
        "Alice", wake_time=time.time(), wake_intent=0.95, override_query="小聲一點",
    )

    ctx = cog._intent_bus.dispatch.await_args.args[0]
    assert ctx.audio_wav_bytes == b"snapshot-audio"


@pytest.mark.asyncio
async def test_no_speech_buffer_entry_yields_none_audio_without_crash():
    cog = _make_cog()
    cog.bot.engine.conv_buffer.get_harvest = MagicMock(return_value="小聲一點")
    # speech_buffers 完全沒有 Alice 的 entry

    await cog._process_queued_query("Alice", wake_time=time.time(), wake_intent=0.95)

    ctx = cog._intent_bus.dispatch.await_args.args[0]
    assert ctx.audio_wav_bytes is None


@pytest.mark.asyncio
async def test_low_confidence_wake_flag_passed_through_to_bus_ctx():
    """wake_intent 低於門檻 → ctx.low_confidence_wake=True，供 rescue 判斷要不要打真的 LLM。"""
    cog = _make_cog()
    cog.bot.engine.conv_buffer.get_harvest = MagicMock(return_value="小聲一點")

    await cog._process_queued_query("Alice", wake_time=time.time(), wake_intent=0.5)

    ctx = cog._intent_bus.dispatch.await_args.args[0]
    assert ctx.low_confidence_wake is True


@pytest.mark.asyncio
async def test_high_confidence_wake_flag_is_false():
    cog = _make_cog()
    cog.bot.engine.conv_buffer.get_harvest = MagicMock(return_value="小聲一點")

    await cog._process_queued_query("Alice", wake_time=time.time(), wake_intent=0.95)

    ctx = cog._intent_bus.dispatch.await_args.args[0]
    assert ctx.low_confidence_wake is False
