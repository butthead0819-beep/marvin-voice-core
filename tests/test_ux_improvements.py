"""
UX 改善測試：
A. 過太久不回 — wake_latency > 25s 時放棄回應（不 TTS、不貼文）
B. 點歌去重 — session 中重複歌曲加入佇列時發出提示並跳過
G. 直接音樂指令 — 無喚醒詞時「點歌」指令也能直接觸發
"""
from __future__ import annotations

import asyncio
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
    bot.router._call_llm = AsyncMock(return_value="我在聽。")
    bot.router._background_intent_enrich = AsyncMock()
    bot.router.memory = MagicMock()
    bot.router.memory.get_player_data.return_value = {}
    bot.router.atmosphere_tracker = None
    bot.router.wake_fusion = None
    bot.engine = MagicMock()
    bot.engine.conv_buffer = MagicMock()
    bot.engine.conv_buffer.get_harvest = MagicMock(return_value="test query")
    bot.engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    bot.engine.post_summon_callback = None

    with patch("discord_voice_engine.faster_whisper", None, create=True):
        from discord_voice_engine import DiscordVoiceEngine
        engine = DiscordVoiceEngine(bot)
    bot.engine_obj = engine

    with patch("discord.ext.tasks.loop", lambda *a, **kw: lambda f: f), \
         patch("cogs.voice_controller.DepartureStats", MagicMock), \
         patch("cogs.voice_controller.ConsentManager", MagicMock):
        from cogs.voice_controller import VoiceController
        cog = VoiceController(bot)

    cog.active_text_channel = AsyncMock()
    _placeholder_msg = MagicMock()
    _placeholder_msg.edit = AsyncMock()
    _placeholder_msg.delete = AsyncMock()
    cog.active_text_channel.send = AsyncMock(return_value=_placeholder_msg)
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
    cog._tts_flush_requested = False
    cog._active_control_view = None
    return cog


# ── A. Late response skip ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_late_response_skip_when_latency_exceeded():
    """首句在 wake 後 30s 才到達 → 放棄回應，不呼叫 play_tts。"""
    cog = _make_cog()
    tts_called = []

    async def _fake_play_tts(text, **kw):
        tts_called.append(text)

    cog.play_tts = _fake_play_tts

    # 模擬 LLM stream 回傳一句話，但 wake_time 設為 30s 前
    fake_wake_time = 1000.0
    fake_now        = 1030.0  # 30s later

    async def _fake_sentence_gen(_stream):
        yield "我在這裡，雖然這對宇宙毫無意義。"

    with patch("time.time", return_value=fake_now), \
         patch.object(cog, "_stream_sentence_splitter", side_effect=_fake_sentence_gen), \
         patch.object(cog, "_cot_filter_stream", side_effect=lambda s: s), \
         patch.object(cog, "_is_low_confidence_answer", return_value=False), \
         patch.object(cog.bot.router, "build_context_prompt", new_callable=AsyncMock, return_value="ctx"), \
         patch.object(cog.bot.router, "stream_response", return_value=_fake_async_gen(["我在這裡，雖然這對宇宙毫無意義。"])):
        await cog._process_queued_query(
            speaker="showay",
            wake_time=fake_wake_time,
        )

    assert len(tts_called) == 0, f"play_tts 不應被呼叫，但被呼叫了 {len(tts_called)} 次"


@pytest.mark.asyncio
async def test_fast_response_not_skipped():
    """首句在 wake 後 5s 到達 → 正常呼叫 play_tts。"""
    cog = _make_cog()
    tts_called = []

    async def _fake_play_tts(text, **kw):
        tts_called.append(text)

    cog.play_tts = _fake_play_tts

    fake_wake_time = 1000.0
    fake_now        = 1005.0  # 5s later

    async def _fake_sentence_gen(_stream):
        yield "我在這裡，雖然這對宇宙毫無意義。"

    with patch("time.time", return_value=fake_now), \
         patch.object(cog, "_stream_sentence_splitter", side_effect=_fake_sentence_gen), \
         patch.object(cog, "_cot_filter_stream", side_effect=lambda s: s), \
         patch.object(cog, "_is_low_confidence_answer", return_value=False), \
         patch.object(cog.bot.router, "build_context_prompt", new_callable=AsyncMock, return_value="ctx"), \
         patch.object(cog.bot.router, "stream_response", return_value=_fake_async_gen(["我在這裡。"])):
        await cog._process_queued_query(
            speaker="showay",
            wake_time=fake_wake_time,
        )
        await asyncio.sleep(0)  # give create_task'd coroutines a chance to run

    assert len(tts_called) >= 1


# ── B. Song deduplication ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_duplicate_song_in_queue_rejected():
    """佇列中已有相同 URL 的歌曲 → 不重複加入，回傳 False。"""
    cog = _make_cog()
    cog.stream_queue = [{"url": "https://yt.com/abc", "title": "末班車", "duration": 200}]

    result = cog._check_song_duplicate(
        url="https://yt.com/abc",
        title="末班車",
        username="showay",
    )
    assert result is True  # is_duplicate = True


@pytest.mark.asyncio
async def test_duplicate_song_in_session_history_rejected():
    """session stream_history 中最近播過的同 URL → 視為重複。"""
    cog = _make_cog()
    cog.stream_history = [{"url": "https://yt.com/abc", "title": "末班車"}]

    result = cog._check_song_duplicate(
        url="https://yt.com/abc",
        title="末班車",
        username="showay",
    )
    assert result is True


@pytest.mark.asyncio
async def test_different_song_not_rejected():
    """不同 URL 的歌曲 → 不視為重複，允許加入。"""
    cog = _make_cog()
    cog.stream_queue = [{"url": "https://yt.com/abc", "title": "末班車"}]

    result = cog._check_song_duplicate(
        url="https://yt.com/xyz",
        title="天空",
        username="showay",
    )
    assert result is False


# ── G. Music direct command without wake word ────────────────────────────────

def test_detect_music_direct_command_recognises_song_request():
    """'我想聽末班車' 應觸發直接點歌，不需要喚醒詞。"""
    cog = _make_cog()
    result = cog._detect_music_direct_command("我想聽末班車", stream_mode=False)
    assert result is not None
    assert result.get("action") in ("play", "search")


def test_detect_music_direct_command_stop_in_stream_mode():
    """'停一下' 在 stream_mode=True 時仍應觸發停止指令。"""
    cog = _make_cog()
    result = cog._detect_music_direct_command("停一下", stream_mode=True)
    assert result is not None
    assert result.get("action") == "stop"


def test_detect_music_direct_command_no_match():
    """普通句子不應觸發直接音樂指令。"""
    cog = _make_cog()
    result = cog._detect_music_direct_command("今天天氣不錯", stream_mode=False)
    assert result is None


def test_pause_playback_triggers_pause_not_stop():
    """'暫停播放' 必須觸發 pause，不能誤判為 stop。"""
    cog = _make_cog()
    result = cog._detect_music_direct_command("暫停播放", stream_mode=True)
    assert result is not None
    assert result.get("action") == "pause"


def test_stop_playback_triggers_stop_not_pause():
    """'停止播放' 必須觸發 stop，不能誤判為 pause。"""
    cog = _make_cog()
    result = cog._detect_music_direct_command("停止播放", stream_mode=True)
    assert result is not None
    assert result.get("action") == "stop"


def test_pause_and_stop_are_distinct_when_no_stream():
    """'暫停播放' 在 stream_mode=False 時也應識別為 pause。"""
    cog = _make_cog()
    result = cog._detect_music_direct_command("暫停播放", stream_mode=False)
    assert result is not None
    assert result.get("action") == "pause"


# ── helpers ──────────────────────────────────────────────────────────────────

async def _fake_async_gen(items):
    for item in items:
        yield item
