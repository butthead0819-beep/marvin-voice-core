"""
VoiceController.speak() — 統一的 stream-aware TTS 入口。

未來新 agent（IntentAgent handler / SpeakAgent handler）要說話只記這個 API，
不用記 play_tts 的 6 個 kwargs 組合。封裝兩件事：
  1. hotswap 接線（allow_hotswap=True, hotswap_max_chars=STREAM_BUDGET）
  2. proactive vs response 的差別（silent_during_stream gating）

預設 proactive=False（喚醒回應 / 對話）；greeting/farewell/idle/ack 等
主動發話應傳 proactive=True。
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


def _make_vc_with_speak():
    """繞過 VoiceController 全建構，直接抓 speak() 綁到 stub 上測。"""
    from cogs.voice_controller import VoiceController

    vc = VoiceController.__new__(VoiceController)
    vc.play_tts = AsyncMock()
    return vc


# ── 1. 預設（response 類）：proactive=False，hotswap 接通 ──────────────────────

@pytest.mark.asyncio
async def test_speak_default_passes_hotswap_kwargs():
    from utterance_budget import STREAM_BUDGET
    vc = _make_vc_with_speak()
    await vc.speak("好啦好啦")
    _, kwargs = vc.play_tts.call_args
    assert kwargs["allow_hotswap"] is True
    assert kwargs["hotswap_max_chars"] == STREAM_BUDGET
    assert kwargs["silent_during_stream"] is False
    assert kwargs["already_in_channel"] is True


# ── 2. proactive=True（greeting/farewell/idle）：silent_during_stream 開 ────────

@pytest.mark.asyncio
async def test_speak_proactive_marks_silent_during_stream():
    vc = _make_vc_with_speak()
    await vc.speak("唉，又是你。", proactive=True)
    _, kwargs = vc.play_tts.call_args
    assert kwargs["silent_during_stream"] is True
    assert kwargs["allow_hotswap"] is True


# ── 3. 自訂 max_chars（短 ack 想用 MAX_HOTSWAP_CHARS=12）──────────────────────

@pytest.mark.asyncio
async def test_speak_accepts_custom_max_chars():
    from cogs.voice_controller import MAX_HOTSWAP_CHARS
    vc = _make_vc_with_speak()
    await vc.speak("收到", max_chars=MAX_HOTSWAP_CHARS)
    _, kwargs = vc.play_tts.call_args
    assert kwargs["hotswap_max_chars"] == MAX_HOTSWAP_CHARS


# ── 4. emotion_tag forward ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_speak_forwards_emotion_tag():
    vc = _make_vc_with_speak()
    await vc.speak("好喔", emotion_tag="amused")
    _, kwargs = vc.play_tts.call_args
    assert kwargs["emotion_tag"] == "amused"


# ── 5. text 是 speak 的第一個位置參數（forward 到 play_tts 第一位）─────────────

@pytest.mark.asyncio
async def test_speak_forwards_text():
    vc = _make_vc_with_speak()
    await vc.speak("阿，又是你。")
    args, _ = vc.play_tts.call_args
    assert args[0] == "阿，又是你。"
