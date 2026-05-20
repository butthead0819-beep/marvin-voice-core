"""TDD: STT inflight slot 必須在 cleaner LLM 之前釋放。

2026-05-20 prod regression：wake_inflight 計數器握著整個 _process_stt_hybrid
（含 Track B clean_stt_text）。Groq 8b 429 慢 cleaner → slot 一直被佔 →
4 人同時講話時第 3+ 個 wake_check 被丟棄，而 Cerebras 整個 idle 卻碰不到。

修法：_process_stt_hybrid 在 STT 完成（_lock.release()）後立刻呼叫
release_inflight，cleaner 在 slot 外跑。
"""
from __future__ import annotations

import asyncio
import wave
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest


def _make_engine():
    bot = MagicMock()
    bot.cogs = {}
    bot.guilds = []
    bot.router = MagicMock()
    bot.router.game_dict_string = ""
    bot.router.google_client = None
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


def _make_wav(tmp_path):
    path = tmp_path / "t.wav"
    samples = np.zeros(int(48000 * 0.5 * 2), dtype=np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(48000)
        w.writeframes(samples.tobytes())
    return str(path)


@pytest.mark.asyncio
async def test_inflight_released_before_cleaner_runs(tmp_path):
    """release_inflight 必須在 clean_stt_text 之前被呼叫。"""
    engine = _make_engine()
    wav_path = _make_wav(tmp_path)
    with open(wav_path, "rb") as f:
        wav_bytes = f.read()

    call_order = []

    # Swift STT 回非喚醒詞長句 → 走 Track B cleaner
    engine._run_swift_stt = AsyncMock(return_value="幫我查佛山有什麼好玩的")

    async def _fake_clean(*a, **kw):
        call_order.append("cleaner")
        return {"text": "幫我查佛山有什麼好玩的", "is_wake": False,
                "wake_intent": None, "wake_threshold": 0.7}
    engine.bot.router.clean_stt_text = _fake_clean

    def _release():
        call_order.append("release")

    await engine._process_stt_hybrid(
        speaker_name="showay", wav_path=wav_path, wav_bytes=wav_bytes,
        timestamp=100.0, prosody_data={"physical_duration": 1.0},
        is_wake_check=False, whisper_audio=None, user_id=999,
        release_inflight=_release,
    )

    assert "release" in call_order, "release_inflight 應被呼叫"
    assert "cleaner" in call_order, "cleaner 應被呼叫"
    assert call_order.index("release") < call_order.index("cleaner"), \
        f"release 必須在 cleaner 之前，實際順序: {call_order}"


@pytest.mark.asyncio
async def test_inflight_release_optional_no_crash(tmp_path):
    """release_inflight=None（未傳）時不該 crash（向後相容）。"""
    engine = _make_engine()
    wav_path = _make_wav(tmp_path)
    with open(wav_path, "rb") as f:
        wav_bytes = f.read()
    engine._run_swift_stt = AsyncMock(return_value="幫我查佛山")
    engine.bot.router.clean_stt_text = AsyncMock(return_value={
        "text": "幫我查佛山", "is_wake": False, "wake_intent": None, "wake_threshold": 0.7})

    # 不傳 release_inflight → 不該 raise
    await engine._process_stt_hybrid(
        speaker_name="showay", wav_path=wav_path, wav_bytes=wav_bytes,
        timestamp=100.0, prosody_data={"physical_duration": 1.0},
        is_wake_check=False, whisper_audio=None, user_id=999,
    )
    engine.stt_callback.assert_awaited()
