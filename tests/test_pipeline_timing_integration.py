"""TDD — MarvinVoicePipeline._flush_audio_to_stt 必須觸發 pipeline_timing 三個階段。

放這支獨立的整合 test 是因為 tests/test_pipeline_timing.py 只測模組本身,
不會察覺到 pipeline.py 沒接 hook 也 module 跑得起來。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_pipeline_with_mocks():
    """Instantiate MarvinVoicePipeline with deps stubbed."""
    bot = MagicMock()
    bot.guilds = []  # speaker name fallback path
    bot.router = MagicMock(game_dict_string="")

    with patch("marvin_voice_core.pipeline.STTHandler", MagicMock()):
        from marvin_voice_core.pipeline import MarvinVoicePipeline
        pipe = MarvinVoicePipeline(bot)

    pipe.stt_handler.transcribe_hybrid = AsyncMock(return_value=("哈囉", "Swift", {}))
    pipe.stt_callback = AsyncMock()
    return pipe


@pytest.mark.asyncio
async def test_process_audio_slice_marks_stt_start_and_done():
    """每次 audio slice 跑完，pipeline_timing 該有 stt_start / stt_done 兩個 marker。"""
    pipe = _make_pipeline_with_mocks()

    # 24000 bytes = 0.125s 48kHz stereo int16 (剛好過 19200 bytes 最低門檻)
    raw_pcm = b"\x10\x00" * 12000

    # 直接 patch save_wav / open / os.remove，避免真碰檔案系統
    with patch("marvin_voice_core.pipeline.save_wav", return_value="/tmp/fake.wav"), \
         patch("marvin_voice_core.pipeline.calculate_rms", return_value=500), \
         patch("builtins.open", new_callable=MagicMock) as mock_open, \
         patch("os.path.exists", return_value=False), \
         patch("os.remove", MagicMock()):
        mock_open.return_value.__enter__.return_value.read.return_value = b"FAKEWAV"

        import pipeline_timing
        await pipe.process_audio_slice(user_id=12345, raw_pcm=raw_pcm, start_time=0.0)
        snap = pipeline_timing.snapshot()

    assert snap is not None, "pipeline_timing.start() 沒在 process_audio_slice 內被呼叫"
    assert "stt_start" in snap, "stt_start mark 缺失 — STT call 前沒打點"
    assert "stt_done" in snap, "stt_done mark 缺失 — STT call 後沒打點"
    # endpoint < stt_start < stt_done 順序
    assert snap["endpoint"] <= snap["stt_start"] <= snap["stt_done"]


@pytest.mark.asyncio
async def test_each_slice_gets_fresh_timing_context():
    """ContextVar per task — 兩個獨立的 audio slice task 應該各自有 timing dict，不交叉。"""
    pipe = _make_pipeline_with_mocks()
    raw_pcm = b"\x10\x00" * 12000

    captured = {}

    async def _run(uid: int, key: str):
        with patch("marvin_voice_core.pipeline.save_wav", return_value="/tmp/fake.wav"), \
             patch("marvin_voice_core.pipeline.calculate_rms", return_value=500), \
             patch("builtins.open", new_callable=MagicMock) as mock_open, \
             patch("os.path.exists", return_value=False), \
             patch("os.remove", MagicMock()):
            mock_open.return_value.__enter__.return_value.read.return_value = b"FAKEWAV"
            await pipe.process_audio_slice(user_id=uid, raw_pcm=raw_pcm, start_time=0.0)
            import pipeline_timing
            captured[key] = pipeline_timing.snapshot()

    await asyncio.gather(
        asyncio.create_task(_run(111, "a")),
        asyncio.create_task(_run(222, "b")),
    )

    assert captured["a"] is not None
    assert captured["b"] is not None
    # 兩條 task 各自的 endpoint 該不同（不同的 dict object）
    assert captured["a"] is not captured["b"]
