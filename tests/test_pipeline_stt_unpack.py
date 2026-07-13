"""回歸：MarvinVoicePipeline._flush_audio_to_stt 解包 transcribe_hybrid 的 3-tuple。

Bug：pipeline.py 只 unpack 2 值（raw_text, engine），但 STTHandler.transcribe_hybrid
自 2026-05-24 起回 3-tuple (text, engine, meta) → ValueError 被 _flush 的 except 吞掉 →
stt_callback 永不被呼叫（STT 靜默失效）。修＝unpack 3、忽略 meta。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_pipeline():
    bot = MagicMock()
    bot.guilds = []
    bot.router = MagicMock(game_dict_string="")
    with patch("marvin_voice_core.pipeline.STTHandler", MagicMock()):
        from marvin_voice_core.pipeline import MarvinVoicePipeline
        pipe = MarvinVoicePipeline(bot)
    pipe.meta_analyzer = None                       # 跳過 prosody 區塊
    pipe.stt_callback = AsyncMock()
    pipe.stt_handler.transcribe_hybrid = AsyncMock(return_value=("你好馬文", "Swift", {}))
    return pipe


@pytest.mark.asyncio
async def test_flush_dispatches_stt_callback_with_three_tuple_return():
    """3-tuple 回傳不該炸；stt_callback 要拿到 raw_text 被呼叫。"""
    pipe = _make_pipeline()
    pipe.audio_buffers["u1"] = {"pcm": bytearray(b"\x01\x00" * 5000), "first_start": 123.0}

    with patch("marvin_voice_core.pipeline.save_wav", return_value="/tmp/fake_stt.wav"), \
         patch("marvin_voice_core.pipeline.os.path.exists", return_value=False), \
         patch("builtins.open", mock_open(read_data=b"WAVBYTES")):
        await pipe._flush_audio_to_stt("u1")

    pipe.stt_callback.assert_awaited_once()
    args = pipe.stt_callback.call_args.args
    assert args[0] == "User_u1"        # speaker（bot.guilds 空 → fallback）
    assert args[1] == "你好馬文"        # raw_text（bug 下因 ValueError 根本不會到這）
