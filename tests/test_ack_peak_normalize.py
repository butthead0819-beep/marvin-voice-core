"""ack 音量修正：peak-normalize f32 到 ~0.9 滿幅，讓 ack 不被 ducked 音樂蓋掉。

背景：ack mp3 本身 mean ~-25dB，進 mixer 再 ×tts_gain 0.5，疊在 duck 到 30% 的
音樂上會偏小。say fallback 早已用 peak_normalize_wav_bytes 補救，_play_ack 的
f32 路徑漏掉，這裡補一個 f32 版本。
"""
import numpy as np

from audio_mixing import peak_normalize_f32


def test_boosts_quiet_to_target():
    arr = np.array([0.5, -0.25, 0.1], dtype=np.float32)
    out = peak_normalize_f32(arr, target_peak=0.9)
    assert abs(float(np.max(np.abs(out))) - 0.9) < 1e-4


def test_silence_unchanged_no_divzero():
    arr = np.zeros(10, dtype=np.float32)
    out = peak_normalize_f32(arr)
    assert np.all(out == 0.0)   # 不得 NaN / div-by-zero


def test_attenuates_hot_to_target():
    # 已滿幅的 ack 也拉到一致響度（避免爆音）
    arr = np.array([1.0, -0.8], dtype=np.float32)
    out = peak_normalize_f32(arr, target_peak=0.9)
    assert abs(float(np.max(np.abs(out))) - 0.9) < 1e-4


def test_empty_returns_empty():
    arr = np.array([], dtype=np.float32)
    out = peak_normalize_f32(arr)
    assert out.size == 0


def test_dtype_preserved():
    arr = np.array([0.3], dtype=np.float32)
    assert peak_normalize_f32(arr).dtype == np.float32
