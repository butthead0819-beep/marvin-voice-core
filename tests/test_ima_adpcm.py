"""IMA ADPCM 編解碼 — puck 麥克風上傳格式的正確性驗證。

驗證重點：編碼/解碼往返的訊號品質、header 格式手算比對、
block 長度檢查、block 間狀態獨立解碼的正確性。
"""
from __future__ import annotations

import struct

import numpy as np
import pytest

from ima_adpcm import BLOCK_BYTES, BLOCK_SAMPLES, ImaAdpcmEncoder, decode_block, decode_stream


def _sine_wave(freq=440.0, amp=8000, rate=16000, n=2048):
    t = np.arange(n) / rate
    return (np.sin(2 * np.pi * freq * t) * amp).astype(np.int16)


def _encode_full(pcm: np.ndarray) -> bytes:
    encoder = ImaAdpcmEncoder()
    out = bytearray()
    n_blocks = len(pcm) // BLOCK_SAMPLES
    for i in range(n_blocks):
        out += encoder.encode_block(pcm[i * BLOCK_SAMPLES:(i + 1) * BLOCK_SAMPLES])
    return bytes(out)


def test_sine_wave_roundtrip_snr():
    pcm = _sine_wave()
    encoded = _encode_full(pcm)
    decoded = decode_stream(encoded)

    assert len(decoded) == len(pcm)

    signal_power = float(np.sum(pcm.astype(np.float64) ** 2))
    noise = pcm.astype(np.float64) - decoded.astype(np.float64)
    noise_power = float(np.sum(noise ** 2))
    snr_db = 10 * np.log10(signal_power / noise_power)

    assert snr_db > 20, f"SNR 太低: {snr_db:.1f}dB"


def test_all_zero_block_decodes_to_zero():
    pcm = np.zeros(BLOCK_SAMPLES, dtype=np.int16)
    encoder = ImaAdpcmEncoder()
    block = encoder.encode_block(pcm)
    decoded = decode_block(block)
    assert np.all(decoded == 0)


def test_encode_block_wrong_length_raises():
    encoder = ImaAdpcmEncoder()
    with pytest.raises(ValueError):
        encoder.encode_block(np.zeros(100, dtype=np.int16))


def test_decode_block_wrong_length_raises():
    with pytest.raises(ValueError):
        decode_block(b"\x00" * 100)


def test_decode_stream_non_multiple_length_raises():
    with pytest.raises(ValueError):
        decode_stream(b"\x00" * (BLOCK_BYTES + 1))


def test_header_format_manual_decode():
    """手算：predictor=1000, index=10, 全部 nibble=0（lo=hi=0）。

    nibble=0 的解碼公式：n=0 → sign bit(0x8)=0, 4=0, 2=0, 1=0
      step = _STEP_TABLE[10] = 19
      diff = step >> 3 = 19 >> 3 = 2（其餘 bit 皆為 0，不加）
      sign=0 → predictor += diff
    第 1 個取樣（lo nibble of byte 0）：predictor = 1000 + 2 = 1002
    index 更新：_INDEX_TABLE[0] = -1 → index = max(0, 10-1) = 9
    第 2 個取樣（hi nibble of byte 0，nibble=0, index=9）：
      step = _STEP_TABLE[9] = 17
      diff = 17 >> 3 = 2
      predictor = 1002 + 2 = 1004
    """
    predictor = 1000
    index = 10
    header = struct.pack("<hBB", predictor, index, 0)
    data = bytes(256)  # 全部 nibble = 0
    block = header + data

    decoded = decode_block(block)
    assert decoded[0] == 1002
    assert decoded[1] == 1004


def test_each_block_independently_decodable():
    pcm = _sine_wave(n=1024)
    encoder = ImaAdpcmEncoder()
    block1 = encoder.encode_block(pcm[:BLOCK_SAMPLES])
    block2 = encoder.encode_block(pcm[BLOCK_SAMPLES:])

    decoded_stream = decode_stream(block1 + block2)
    decoded_block2_alone = decode_block(block2)

    assert np.array_equal(decoded_block2_alone, decoded_stream[BLOCK_SAMPLES:])
