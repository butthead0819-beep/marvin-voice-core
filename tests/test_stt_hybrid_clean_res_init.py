"""
TDD：Layer 3 STT Sx1 — _process_stt_hybrid 內 clean_res 必須預先初始化，
避免 router 未提供 clean_stt_text 時走到 should_callback 路徑時 NameError。

問題：原本 clean_res 只在 `if hasattr(self.bot.router, 'clean_stt_text'):` 內賦值，
但下方 `should_callback = True` 仍可能從 `elif not is_wake_check:` 觸發。
若 router 缺 clean_stt_text → clean_res 未定義 → L1322
  `_b_wake_intent = clean_res.get("wake_intent") if isinstance(clean_res, dict) else None`
中 `isinstance(undefined_var, dict)` 直接 NameError 而非回 None。

修法：clean_res 預先 init 為 None，與 cleaned_text / is_wake_B 一同預設。
"""
from __future__ import annotations

import asyncio
import os
import wave
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest


def _make_engine():
    """裝一個極簡的 DiscordVoiceEngine，router 故意不掛 clean_stt_text。"""
    bot = MagicMock()
    bot.cogs = {}
    bot.guilds = []
    bot.router = MagicMock()
    # 故意不掛 clean_stt_text → engine 走 cleaner-skip 路徑
    if hasattr(bot.router, "clean_stt_text"):
        del bot.router.clean_stt_text
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
    """生一個短暫的 WAV 檔讓 _process_stt_hybrid 不會在 file IO 階段炸。"""
    path = tmp_path / "test_stt.wav"
    samples = np.zeros(int(48000 * duration_sec * 2), dtype=np.int16)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(samples.tobytes())
    return str(path)


@pytest.mark.asyncio
async def test_clean_res_undefined_does_not_raise_when_callback_path_taken(tmp_path):
    """router 沒 clean_stt_text 時，should_callback 路徑不該因 clean_res 未定義 NameError。

    模擬路徑：non-wake-check + raw_text 長度 > 3 → 觸發 elif not is_wake_check 那條
    → 跑到 L1322 _b_wake_intent = clean_res.get(...) → 若沒初始化會 NameError
    """
    engine = _make_engine()
    wav_path = _make_wav_path(tmp_path)
    with open(wav_path, "rb") as f:
        wav_bytes = f.read()

    # Mock Swift STT 直接回非空字串，讓我們走到 Track A / Track B path
    engine._run_swift_stt = AsyncMock(return_value="今天天氣不錯適合出去走走")  # 非喚醒詞、長度 > 3

    # 不該 NameError；callback 應該被呼叫（Track B 路徑因為 not is_wake_check + len > 3）
    await engine._process_stt_hybrid(
        speaker_name="Alice",
        wav_path=wav_path,
        wav_bytes=wav_bytes,
        timestamp=100.0,
        prosody_data={"physical_duration": 1.0},
        is_wake_check=False,
        whisper_audio=None,
        user_id=12345,
    )

    # callback 應該被呼叫且 wake_intent 為 None（沒 cleaner → 沒 wake_intent）
    engine.stt_callback.assert_awaited()
    # 找到 Track B 那次呼叫（track="B"）並驗證 wake_intent=None
    track_b_calls = [
        c for c in engine.stt_callback.await_args_list
        if c.kwargs.get("track") == "B"
    ]
    assert track_b_calls, "Track B callback 應被觸發"
    assert track_b_calls[0].kwargs.get("wake_intent") is None
