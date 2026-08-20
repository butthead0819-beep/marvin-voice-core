"""測試 LocalMixingSource 的音量平滑漸變（Volume Ramp），避免 Auto Gain 或手動調音量時產生 step jump/click。"""
from __future__ import annotations

import numpy as np
import pytest

from local_mixing_source import FRAME_BYTES_F32, FRAME_SAMPLES, LocalMixingAudioSource


class DummyMusicSource:
    """產生常數 1.0 的立體聲 f32 frame"""
    def __init__(self, frames: int = 100):
        self.frames_left = frames

    def read(self) -> bytes:
        if self.frames_left <= 0:
            return b""
        self.frames_left -= 1
        # Stereo f32 filled with 1.0
        arr = np.ones(FRAME_SAMPLES * 2, dtype=np.float32)
        return arr.tobytes()

    def cleanup(self):
        pass


def test_volume_ramps_smoothly():
    mixer = LocalMixingAudioSource(on_demand=False)
    mixer.set_music_source(DummyMusicSource(100))
    mixer.set_volume(1.0)
    
    # 讀取第 1 幀（此時 volume = 1.0）
    f1 = mixer.read()
    arr1 = np.frombuffer(f1, dtype=np.int16).astype(np.float32) / 32767.0
    assert np.mean(arr1) == pytest.approx(1.0, abs=0.05)

    # 突然設定音量降為 0.5
    mixer.set_volume(0.5)

    # 下一幀不應該瞬間腰斬到 0.5，而是平滑逼近
    frames = [mixer.read() for _ in range(30)]
    levels = [np.mean(np.frombuffer(f, dtype=np.int16).astype(np.float32) / 32767.0) for f in frames]

    # 確認 levels 是單調遞減直到接近 0.5
    assert levels[0] < arr1.mean()
    assert levels[-1] == pytest.approx(0.5, abs=0.05)
    # 確認不是第一幀就直接跳到 0.5
    assert levels[0] > 0.55
