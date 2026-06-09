"""TDD: NemoClaw TTS silent-fail 修復

stream_audio 在 edge-tts 回傳空 chunk（無 audio type）時應視為失敗、走 fallback，
而非靜默回傳空流。

修復前問題：
  primary edge-tts 完成但未 yield 任何 audio chunk
  → success = True（誤判）
  → 不走 secondary / macOS fallback
  → ffmpeg stdin 立即關閉，_drain() 拿不到 frame
  → 完全無聲，也無任何 log
"""
from __future__ import annotations
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


async def _empty_stream():
    """edge-tts Communicate.stream() 模擬：只回 metadata，無 audio chunk。"""
    yield {"type": "audio.metadata", "data": {}}  # metadata only, no audio


async def _good_stream():
    """正常的 edge-tts stream：回傳 audio chunk。"""
    yield {"type": "audio", "data": b"\x00\x01\x02\x03"}


# ── 直接測 SukiTTS.stream_audio ───────────────────────────────────────────────

@pytest.fixture
def tts():
    from tts_engine import SukiTTS
    return SukiTTS()


@pytest.mark.asyncio
async def test_stream_audio_zero_chunk_triggers_fallback(tts):
    """primary 完成但 0 audio chunk → 不應靜默結束，應走 secondary fallback。"""
    secondary_called = []

    async def _empty():
        yield {"type": "audio.metadata", "data": {}}

    async def _secondary_good():
        yield {"type": "audio", "data": b"\xAA\xBB"}

    with patch("edge_tts.Communicate") as MockComm:
        instances = []

        def side_effect(*args, **kwargs):
            m = MagicMock()
            # 第一次呼叫（primary）回傳空 stream；第二次（secondary）回傳正常 audio
            if len(instances) == 0:
                m.stream = _empty
            else:
                m.stream = _secondary_good
                secondary_called.append(True)
            instances.append(m)
            return m

        MockComm.side_effect = side_effect

        chunks = [chunk async for chunk in tts.stream_audio("測試文字")]

    assert secondary_called, "primary 0-chunk 未觸發 secondary fallback"
    assert len(chunks) > 0, "fallback 應回傳 audio chunks"


@pytest.mark.asyncio
async def test_stream_audio_normal_success_no_fallback(tts):
    """primary 正常回傳 audio chunk → success，不走 fallback。"""
    secondary_called = []

    async def _primary_good():
        yield {"type": "audio", "data": b"\x00\x01"}

    with patch("edge_tts.Communicate") as MockComm:
        instances = []

        def side_effect(*args, **kwargs):
            m = MagicMock()
            if len(instances) == 0:
                m.stream = _primary_good
            else:
                secondary_called.append(True)
                m.stream = _primary_good
            instances.append(m)
            return m

        MockComm.side_effect = side_effect

        chunks = [chunk async for chunk in tts.stream_audio("測試文字")]

    assert not secondary_called, "primary 成功時不應走 secondary"
    assert len(chunks) > 0


@pytest.mark.asyncio
async def test_stream_audio_logs_zero_chunk_warning(tts, caplog):
    """primary 0-chunk 應記錄警告，讓 ops 可診斷。"""
    import logging

    async def _empty():
        yield {"type": "audio.metadata", "data": {}}

    async def _secondary_good():
        yield {"type": "audio", "data": b"\xAA"}

    with patch("edge_tts.Communicate") as MockComm:
        instances = []

        def side_effect(*args, **kwargs):
            m = MagicMock()
            if len(instances) == 0:
                m.stream = _empty
            else:
                m.stream = _secondary_good
            instances.append(m)
            return m

        MockComm.side_effect = side_effect

        with caplog.at_level(logging.WARNING):
            _ = [chunk async for chunk in tts.stream_audio("測試文字")]

    # 應有警告說「no audio」或「空流」
    assert any("audio" in r.message.lower() or "chunk" in r.message.lower()
               or "fallback" in r.message.lower() or "零" in r.message
               or "0 chunk" in r.message or "zero" in r.message.lower()
               for r in caplog.records), (
        f"缺少 zero-chunk 警告 log，現有: {[r.message for r in caplog.records]}"
    )
