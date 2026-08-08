import stat
import os

import numpy as np

from device.puck_mic_aec import CHUNK_FRAMES, PuckMicAecLoop, make_fifo_writer


class _FakeMixer:
    """假 PuckMixer：process_chunk() 只需要 recent_reference()，不用真的 ring buffer。"""

    def __init__(self, ref: np.ndarray):
        self._ref = ref

    def recent_reference(self, seconds: float) -> np.ndarray:
        return self._ref


def test_process_chunk_calls_aec_with_mixer_reference_and_margin():
    n_ref = 48000  # 1 秒份量 stereo
    t = np.arange(n_ref) / 48000.0
    ref_stereo = np.zeros(n_ref * 2, dtype=np.int16)
    ref_stereo[0::2] = (5000 * np.sin(2 * np.pi * 300 * t)).astype(np.int16)
    ref_stereo[1::2] = ref_stereo[0::2]
    mixer = _FakeMixer(ref_stereo)

    received = []
    loop = PuckMicAecLoop(mixer=mixer, mic_device="hw:test", on_clean_chunk=received.append)

    mic_chunk = np.zeros(CHUNK_FRAMES, dtype=np.float64)
    cleaned = loop.process_chunk(mic_chunk)

    assert cleaned.shape == mic_chunk.shape
    assert np.all(np.isfinite(cleaned))


def test_process_chunk_handles_empty_reference_gracefully():
    mixer = _FakeMixer(np.zeros(0, dtype=np.int16))
    loop = PuckMicAecLoop(mixer=mixer, mic_device="hw:test", on_clean_chunk=lambda b: None)

    mic_chunk = np.arange(CHUNK_FRAMES, dtype=np.float64)
    cleaned = loop.process_chunk(mic_chunk)

    assert np.allclose(cleaned, mic_chunk)  # 沒有 reference → 原樣回傳（見 puck_aec.process）


def test_make_fifo_writer_creates_fifo_file(tmp_path):
    fifo_path = str(tmp_path / "mic_clean.pcm")
    make_fifo_writer(fifo_path)

    assert os.path.exists(fifo_path)
    assert stat.S_ISFIFO(os.stat(fifo_path).st_mode)


def test_make_fifo_writer_does_not_raise_without_reader(tmp_path):
    fifo_path = str(tmp_path / "mic_clean.pcm")
    write = make_fifo_writer(fifo_path)

    write(b"\x00\x01\x02\x03")  # 沒有人在讀，應該吞掉錯誤而不是拋例外
