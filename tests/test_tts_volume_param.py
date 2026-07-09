"""TDD — T6 stream_audio volume param threading（先紅後綠）。

stream_audio 增加 volume 參數，預設 None（byte-equivalent）；
傳值時轉進 edge_tts.Communicate 的 volume kwarg。
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch


async def _consume(agen):
    """Drain an async generator, return list of yielded items."""
    chunks = []
    async for c in agen:
        chunks.append(c)
    return chunks


def _make_fake_communicate():
    """回傳 (communicate_mock, call_kwargs_ref)。

    communicate_mock 被呼叫時記下 kwargs；.stream() 回傳一個 audio chunk。
    """
    captured = {}

    async def fake_stream():
        yield {"type": "audio", "data": b"FAKE_AUDIO"}

    def side_effect(**kwargs):
        captured.update(kwargs)
        m = MagicMock()
        m.stream = fake_stream
        return m

    return side_effect, captured


@pytest.mark.asyncio
async def test_stream_audio_no_volume_does_not_pass_volume_to_edge_tts():
    """stream_audio(text) 無 volume → edge_tts.Communicate 不收到 volume kwarg（byte-equiv）。"""
    from tts_engine import SukiTTS
    engine = SukiTTS()

    side_effect, captured = _make_fake_communicate()
    with patch("edge_tts.Communicate", side_effect=side_effect):
        await _consume(engine.stream_audio("嗨"))

    assert "volume" not in captured, f"預期無 volume kwarg，實際 captured={captured}"


@pytest.mark.asyncio
async def test_stream_audio_with_volume_passes_volume_to_edge_tts():
    """stream_audio(text, volume='-20%') → edge_tts.Communicate 收到 volume='-20%'。"""
    from tts_engine import SukiTTS
    engine = SukiTTS()

    side_effect, captured = _make_fake_communicate()
    with patch("edge_tts.Communicate", side_effect=side_effect):
        await _consume(engine.stream_audio("嗨", volume="-20%"))

    assert captured.get("volume") == "-20%", f"預期 volume='-20%'，實際 captured={captured}"
