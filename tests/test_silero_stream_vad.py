"""SileroStreamVAD — 真 ONNX 模型推理測試。

模型檔不存在就 skip（這個模組依賴 faster-whisper 內附的 silero_vad_v6.onnx，
在沒有裝 faster-whisper 資產的環境上不該讓測試紅）。
"""
from __future__ import annotations

import os

import numpy as np
import pytest

silero_stream_vad = pytest.importorskip("silero_stream_vad")

try:
    import faster_whisper.utils as _fw_utils
    _MODEL_PATH = os.path.join(_fw_utils.get_assets_path(), "silero_vad_v6.onnx")
except Exception:
    _MODEL_PATH = None

pytestmark = pytest.mark.skipif(
    not _MODEL_PATH or not os.path.exists(_MODEL_PATH),
    reason="silero_vad_v6.onnx 模型檔不存在",
)


def test_all_zero_audio_gives_low_probability():
    vad = silero_stream_vad.SileroStreamVAD()
    pcm = np.zeros(16000, dtype=np.int16)
    probs = vad.process(pcm)
    assert len(probs) == 16000 // silero_stream_vad.SileroStreamVAD.WINDOW
    assert all(p < 0.2 for p in probs)


def test_segmented_feed_matches_single_feed():
    rng = np.random.RandomState(0)
    pcm = rng.randint(-5000, 5000, size=100 + 412 + 512 + 512 + 300, dtype=np.int16)

    vad_full = silero_stream_vad.SileroStreamVAD()
    probs_full = vad_full.process(pcm)

    vad_chunked = silero_stream_vad.SileroStreamVAD()
    chunk_sizes = [100, 412, 512, 512, 300]
    probs_chunked = []
    offset = 0
    for size in chunk_sizes:
        probs_chunked.extend(vad_chunked.process(pcm[offset:offset + size]))
        offset += size

    assert len(probs_full) == len(probs_chunked)
    assert np.allclose(probs_full, probs_chunked, atol=1e-5)
