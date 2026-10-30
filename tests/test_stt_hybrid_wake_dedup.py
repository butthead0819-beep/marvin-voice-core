"""
TDD：P0-1 — _process_stt_hybrid Track A/B 去重與 WPS 先行計算。

測試場景：
1. 當 is_wake_check=False 且 raw_text="馬文停止播放"（命中 Track A）時：
   - stt_callback 只被 await 剛好一次（track="A"）
   - 不會再被 track="B" 重複呼叫
   - 傳給 stt_callback 的 prosody_data["wps"] > 0，且 char_count 正確
2. 當 is_wake_check=False 且 raw_text="今天天氣真好"（未命中 Track A，長度 > 3）時：
   - 維持走 track="B" 呼叫 stt_callback 一次
   - prosody_data["wps"] > 0
"""
from __future__ import annotations

import os
import wave
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest


def _make_engine():
    bot = MagicMock()
    bot.cogs = {}
    bot.guilds = []
    bot.router = MagicMock()
    bot.router.google_client = None
    bot.router.game_dict_string = ""
    with patch("discord_voice_engine.faster_whisper", None, create=True):
        from discord_voice_engine import DiscordVoiceEngine
        engine = DiscordVoiceEngine(bot)
    engine.whisper_model = None
    engine.conv_buffer = MagicMock()
    engine.conv_buffer.get_last_n_utterances = MagicMock(return_value=[])
    engine.conv_buffer.get_harvest = MagicMock(return_value="")
    engine.meta_analyzer = MagicMock()
    engine.meta_analyzer.calculate_prosody = MagicMock(return_value={"physical_duration": 1.0})
    engine.stt_callback = AsyncMock()
    return engine


def _make_wav_path(tmp_path, duration_sec=0.5):
    path = tmp_path / "test_dedup.wav"
    samples = np.zeros(int(48000 * duration_sec * 2), dtype=np.int16)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(samples.tobytes())
    return str(path)


@pytest.mark.asyncio
async def test_process_stt_hybrid_wake_track_a_no_duplicate_callback_and_has_wps(tmp_path):
    engine = _make_engine()
    wav_path = _make_wav_path(tmp_path)
    with open(wav_path, "rb") as f:
        wav_bytes = f.read()

    # Swift STT 回傳喚醒指令
    raw_text = "馬文停止播放"
    engine._run_swift_stt = AsyncMock(return_value=(raw_text, {}))

    prosody = {"physical_duration": 1.0, "wps": 0.0, "char_count": 0}

    await engine._process_stt_hybrid(
        speaker_name="Alice",
        wav_path=wav_path,
        wav_bytes=wav_bytes,
        timestamp=100.0,
        prosody_data=prosody,
        is_wake_check=False,
        whisper_audio=None,
        user_id=12345,
    )

    # 驗證 stt_callback 僅被呼叫一次，且為 Track A
    assert engine.stt_callback.await_count == 1
    call_args, call_kwargs = engine.stt_callback.call_args
    assert call_args[0] == "Alice"
    assert call_args[1] == raw_text
    assert call_kwargs.get("track") == "A"
    assert call_kwargs.get("prosody_data") is not None
    assert call_kwargs["prosody_data"]["wps"] > 0
    assert call_kwargs["prosody_data"]["char_count"] == len(raw_text.replace(" ", ""))


@pytest.mark.asyncio
async def test_process_stt_hybrid_non_wake_falls_through_to_track_b_once(tmp_path):
    engine = _make_engine()
    wav_path = _make_wav_path(tmp_path)
    with open(wav_path, "rb") as f:
        wav_bytes = f.read()

    # Swift STT 回傳一般非喚醒長句
    raw_text = "今天天氣真好想要出去玩"
    engine._run_swift_stt = AsyncMock(return_value=(raw_text, {}))

    prosody = {"physical_duration": 2.0, "wps": 0.0, "char_count": 0}

    await engine._process_stt_hybrid(
        speaker_name="Bob",
        wav_path=wav_path,
        wav_bytes=wav_bytes,
        timestamp=200.0,
        prosody_data=prosody,
        is_wake_check=False,
        whisper_audio=None,
        user_id=67890,
    )

    # 驗證 stt_callback 僅被呼叫一次，且為 Track B
    assert engine.stt_callback.await_count == 1
    call_args, call_kwargs = engine.stt_callback.call_args
    assert call_args[0] == "Bob"
    assert call_args[1] == raw_text
    assert call_kwargs.get("track") == "B"
    assert call_kwargs.get("prosody_data") is not None
    assert call_kwargs["prosody_data"]["wps"] > 0
    assert call_kwargs["prosody_data"]["char_count"] == len(raw_text.replace(" ", ""))
