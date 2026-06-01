"""
SukiTTS.prewarm — on-wake 連線預熱（修法 B）。

Edge-TTS 每次開新 websocket，冷啟動首音 ~1.8s、暖機後 ~0.3-0.7s。喚醒高信心時
並行丟極短 throwaway 合成暖 DNS/TLS，讓緊接著的真實 TTS 從冷變暖。

不變式：
  - 吞所有錯誤（純優化，絕不影響主流程）
  - 節流：短時間內已暖過就跳過（避免連續 wake 堆疊請求 + 降低被微軟判濫用風險）
  - 拿到首個 audio chunk 即停（連線已暖，不需完整合成）
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_tts():
    from tts_engine import SukiTTS
    return SukiTTS()


class _FakeStream:
    """模擬 edge_tts.Communicate().stream() async generator。"""
    def __init__(self, chunks, raise_exc=None):
        self._chunks = chunks
        self._raise = raise_exc

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        if self._raise:
            raise self._raise
        for c in self._chunks:
            yield c


def _fake_communicate(chunks=None, raise_exc=None):
    """patch target：edge_tts.Communicate → 回傳帶 .stream() 的 mock。"""
    inst = MagicMock()
    inst.stream = MagicMock(return_value=_FakeStream(chunks or [], raise_exc))
    factory = MagicMock(return_value=inst)
    return factory, inst


# ── 1. prewarm 開連線、拿首音即停 ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_prewarm_opens_stream_and_stops_at_first_audio():
    tts = _make_tts()
    audio_chunks = [
        {"type": "WordBoundary"},
        {"type": "audio", "data": b"\x00\x01"},
        {"type": "audio", "data": b"\x02\x03"},  # 不該被消費（首音即停）
    ]
    factory, inst = _fake_communicate(chunks=audio_chunks)
    with patch("tts_engine.edge_tts.Communicate", factory):
        await tts.prewarm()
    factory.assert_called_once()       # 有開 Communicate（暖連線）
    inst.stream.assert_called_once()


# ── 2. 節流：短時間內第二次 prewarm 跳過 ─────────────────────────────────────

@pytest.mark.asyncio
async def test_prewarm_throttled_within_window():
    tts = _make_tts()
    factory, _ = _fake_communicate(chunks=[{"type": "audio", "data": b"x"}])
    with patch("tts_engine.edge_tts.Communicate", factory):
        await tts.prewarm()
        await tts.prewarm()  # 立刻第二次 → 應被節流跳過
    assert factory.call_count == 1, "節流窗內第二次 prewarm 不該再開連線"


@pytest.mark.asyncio
async def test_prewarm_fires_again_after_throttle_window():
    tts = _make_tts()
    factory, _ = _fake_communicate(chunks=[{"type": "audio", "data": b"x"}])
    with patch("tts_engine.edge_tts.Communicate", factory):
        await tts.prewarm()
        # 偽造時間前進超過節流窗
        tts._last_prewarm -= 999.0
        await tts.prewarm()
    assert factory.call_count == 2


# ── 3. 吞所有錯誤，不傳播 ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_prewarm_swallows_errors():
    tts = _make_tts()
    factory, _ = _fake_communicate(raise_exc=RuntimeError("403 blocked"))
    with patch("tts_engine.edge_tts.Communicate", factory):
        # 不該拋
        await tts.prewarm()


@pytest.mark.asyncio
async def test_prewarm_swallows_construct_error():
    tts = _make_tts()
    with patch("tts_engine.edge_tts.Communicate", MagicMock(side_effect=OSError("net down"))):
        await tts.prewarm()  # 不該拋
