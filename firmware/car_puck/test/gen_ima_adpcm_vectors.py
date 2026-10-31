#!/usr/bin/env python3
"""gen_ima_adpcm_vectors.py — 用 ima_adpcm.py 的 ImaAdpcmEncoder 產生 test_ima_adpcm.cpp
用的測試向量，寫到同目錄的 ima_adpcm_vectors.h（兩個檔都 commit）。

執行：
  venv_simon/bin/python firmware/car_puck/test/gen_ima_adpcm_vectors.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import numpy as np

from ima_adpcm import BLOCK_SAMPLES, ImaAdpcmEncoder

OUT_PATH = os.path.join(os.path.dirname(__file__), "ima_adpcm_vectors.h")


def case_silence(n_blocks: int) -> list[np.ndarray]:
    return [np.zeros(BLOCK_SAMPLES, dtype=np.int16) for _ in range(n_blocks)]


def case_sine(n_blocks: int, freq_hz: float, amp: float, rate: int = 16000) -> list[np.ndarray]:
    total = BLOCK_SAMPLES * n_blocks
    t = np.arange(total, dtype=np.float64) / rate
    wave = (amp * np.sin(2 * np.pi * freq_hz * t)).astype(np.int16)
    return [wave[i * BLOCK_SAMPLES:(i + 1) * BLOCK_SAMPLES] for i in range(n_blocks)]


def case_square_fullscale(n_blocks: int, period: int = 32) -> list[np.ndarray]:
    total = BLOCK_SAMPLES * n_blocks
    idx = np.arange(total)
    phase = (idx % period) < (period // 2)
    wave = np.where(phase, 32767, -32768).astype(np.int16)
    return [wave[i * BLOCK_SAMPLES:(i + 1) * BLOCK_SAMPLES] for i in range(n_blocks)]


def case_step_to_max_index(n_blocks: int) -> list[np.ndarray]:
    total = BLOCK_SAMPLES * n_blocks
    wave = np.empty(total, dtype=np.int16)
    wave[:16] = 0
    wave[16:] = 32767
    if total > 32:
        # 再跳一次到谷底，確保後續 block 也能撞到 index 邊界的另一側
        half = total // 2
        wave[half:] = -32768
    return [wave[i * BLOCK_SAMPLES:(i + 1) * BLOCK_SAMPLES] for i in range(n_blocks)]


def case_white_noise(n_blocks: int, seed: int = 1234) -> list[np.ndarray]:
    rng = np.random.default_rng(seed)
    total = BLOCK_SAMPLES * n_blocks
    wave = rng.integers(-32768, 32768, size=total, dtype=np.int32).astype(np.int16)
    return [wave[i * BLOCK_SAMPLES:(i + 1) * BLOCK_SAMPLES] for i in range(n_blocks)]


CASES: list[tuple[str, list[np.ndarray]]] = [
    ("silence", case_silence(2)),
    ("sine440", case_sine(3, 440.0, 8000.0)),
    ("square_fullscale", case_square_fullscale(2)),
    ("step_to_max_index", case_step_to_max_index(2)),
    ("white_noise", case_white_noise(3)),
]


def format_int16_array(name: str, blocks: list[np.ndarray]) -> str:
    flat = np.concatenate(blocks)
    vals = ", ".join(str(int(v)) for v in flat)
    return f"static const int16_t {name}[{len(flat)}] = {{{vals}}};\n"


def format_uint8_array(name: str, data: bytes) -> str:
    vals = ", ".join(str(b) for b in data)
    return f"static const uint8_t {name}[{len(data)}] = {{{vals}}};\n"


def main() -> None:
    lines = [
        "// ima_adpcm_vectors.h — 由 gen_ima_adpcm_vectors.py 產生，勿手改。\n",
        "// 重新產生：venv_simon/bin/python firmware/car_puck/test/gen_ima_adpcm_vectors.py\n",
        "#ifndef IMA_ADPCM_VECTORS_H\n",
        "#define IMA_ADPCM_VECTORS_H\n",
        "\n",
        "#include <stdint.h>\n",
        "\n",
        f"#define IMA_VEC_NUM_CASES {len(CASES)}\n",
        "\n",
    ]

    case_names = []
    for name, blocks in CASES:
        n_blocks = len(blocks)
        pcm_name = f"IMA_VEC_{name}_pcm"
        enc_name = f"IMA_VEC_{name}_encoded"
        lines.append(f"#define IMA_VEC_{name}_NBLOCKS {n_blocks}\n")
        lines.append(format_int16_array(pcm_name, blocks))

        encoder = ImaAdpcmEncoder()
        encoded = b"".join(encoder.encode_block(b) for b in blocks)
        lines.append(format_uint8_array(enc_name, encoded))
        lines.append("\n")
        case_names.append(name)

    lines.append("#endif  // IMA_ADPCM_VECTORS_H\n")

    with open(OUT_PATH, "w") as f:
        f.writelines(lines)

    print(f"wrote {OUT_PATH}: cases={case_names}")


if __name__ == "__main__":
    main()
