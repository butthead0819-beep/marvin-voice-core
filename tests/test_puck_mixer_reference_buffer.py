import numpy as np

from device.puck_mixer import CHANNELS, RATE, PuckMixer


def _stereo_chunk(n_frames: int, value: int) -> np.ndarray:
    return np.full(n_frames * CHANNELS, value, dtype=np.int16)


def test_recent_reference_empty_before_any_write():
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")
    assert mixer.recent_reference(1.0).size == 0


def test_recent_reference_returns_written_chunk_in_order():
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")
    chunk1 = _stereo_chunk(100, 1)
    chunk2 = _stereo_chunk(100, 2)
    mixer._append_reference(chunk1)
    mixer._append_reference(chunk2)

    out = mixer.recent_reference(seconds=200 / RATE)
    assert np.array_equal(out, np.concatenate([chunk1, chunk2]))


def test_recent_reference_wraps_around_ring_buffer():
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")
    mixer._ref_frames = 150  # 縮小 buffer 方便測試 wrap-around
    mixer._ref_ring = np.zeros(mixer._ref_frames * CHANNELS, dtype=np.int16)

    mixer._append_reference(_stereo_chunk(100, 1))
    mixer._append_reference(_stereo_chunk(100, 2))  # 觸發 wrap（100+100 > 150）

    out = mixer.recent_reference(seconds=150 / RATE)
    assert len(out) == 150 * CHANNELS
    # 最新 100 frame 應該是值 2，剩下 50 frame 是還沒被蓋掉的值 1
    tail_frames = out.reshape(-1, CHANNELS)
    assert np.all(tail_frames[-100:] == 2)
    assert np.all(tail_frames[:50] == 1)


def test_recent_reference_caps_at_buffer_size():
    mixer = PuckMixer(bt_mac="AA:BB:CC:DD:EE:FF")
    mixer._ref_frames = 50
    mixer._ref_ring = np.zeros(mixer._ref_frames * CHANNELS, dtype=np.int16)
    mixer._append_reference(_stereo_chunk(200, 3))

    out = mixer.recent_reference(seconds=10.0)  # 遠超過 buffer 容量
    assert len(out) == 50 * CHANNELS
