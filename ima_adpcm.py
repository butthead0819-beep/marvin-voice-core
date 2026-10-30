"""IMA ADPCM 編解碼 — puck 麥克風上傳格式。

封包格式（韌體 C++ 版照這個實作，格式定死）：
  一個 block = 4 bytes header + 256 bytes data = 260 bytes，代表 512 個
  int16 取樣（32ms @16kHz）。

  header: int16 LE predictor（編碼這個 block 之前的預測值）、
          uint8 step_index（0..88，block 之前的狀態）、uint8 reserved=0。
  data:   512 個 4-bit nibble，每個 byte 低 nibble 先
          （第 2k 個取樣在 byte k 的低 4 bit，第 2k+1 個在高 4 bit）。

每個 block 自帶狀態，可獨立解碼。
"""
from __future__ import annotations

import struct

import numpy as np

BLOCK_SAMPLES = 512
BLOCK_BYTES = 260

_STEP_TABLE = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17,
    19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118,
    130, 143, 157, 173, 190, 209, 230, 253, 279, 307,
    337, 371, 408, 449, 494, 544, 598, 658, 724, 796,
    876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066,
    2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358,
    5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899,
    15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
]

_INDEX_TABLE = [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8]


def _decode_nibble(n: int, predictor: int, index: int) -> tuple[int, int]:
    step = _STEP_TABLE[index]
    diff = step >> 3
    if n & 4:
        diff += step
    if n & 2:
        diff += step >> 1
    if n & 1:
        diff += step >> 2
    if n & 8:
        predictor -= diff
    else:
        predictor += diff
    predictor = max(-32768, min(32767, predictor))
    index = max(0, min(88, index + _INDEX_TABLE[n]))
    return predictor, index


class ImaAdpcmEncoder:
    """逐 block 編碼，狀態延續到下一個 block。"""

    def __init__(self) -> None:
        self.predictor = 0
        self.index = 0

    def encode_block(self, pcm: np.ndarray) -> bytes:
        if len(pcm) != BLOCK_SAMPLES:
            raise ValueError(f"pcm 長度必須是 {BLOCK_SAMPLES}，got {len(pcm)}")

        header_predictor = self.predictor
        header_index = self.index

        predictor = self.predictor
        index = self.index
        nibbles = np.empty(BLOCK_SAMPLES, dtype=np.uint8)

        samples = pcm.astype(np.int64)
        for i in range(BLOCK_SAMPLES):
            sample = int(samples[i])
            diff = sample - predictor
            sign = 0x8 if diff < 0 else 0
            diff = abs(diff)

            step = _STEP_TABLE[index]
            n = 0
            if diff >= step:
                n |= 4
                diff -= step
            step_half = step >> 1
            if diff >= step_half:
                n |= 2
                diff -= step_half
            step_quarter = step >> 2
            if diff >= step_quarter:
                n |= 1
            n |= sign

            predictor, index = _decode_nibble(n, predictor, index)
            nibbles[i] = n

        self.predictor = predictor
        self.index = index

        data = bytearray(256)
        for k in range(256):
            lo = nibbles[2 * k] & 0x0F
            hi = nibbles[2 * k + 1] & 0x0F
            data[k] = lo | (hi << 4)

        header = struct.pack("<hBB", header_predictor, header_index, 0)
        return header + bytes(data)


def decode_block(block: bytes) -> np.ndarray:
    if len(block) != BLOCK_BYTES:
        raise ValueError(f"block 長度必須是 {BLOCK_BYTES}，got {len(block)}")

    predictor, index, _reserved = struct.unpack("<hBB", block[:4])
    data = block[4:]

    out = np.empty(BLOCK_SAMPLES, dtype=np.int16)
    for k in range(256):
        byte = data[k]
        lo = byte & 0x0F
        hi = (byte >> 4) & 0x0F

        predictor, index = _decode_nibble(lo, predictor, index)
        out[2 * k] = predictor

        predictor, index = _decode_nibble(hi, predictor, index)
        out[2 * k + 1] = predictor

    return out


def decode_stream(data: bytes) -> np.ndarray:
    if len(data) % BLOCK_BYTES != 0:
        raise ValueError(f"data 長度必須是 {BLOCK_BYTES} 的倍數，got {len(data)}")

    n_blocks = len(data) // BLOCK_BYTES
    chunks = [decode_block(data[i * BLOCK_BYTES:(i + 1) * BLOCK_BYTES]) for i in range(n_blocks)]
    if not chunks:
        return np.empty(0, dtype=np.int16)
    return np.concatenate(chunks)
