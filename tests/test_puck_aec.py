import numpy as np

from device.puck_aec import (
    PuckAecProcessor,
    downmix_stereo_to_mono,
    estimate_delay_samples,
    resample_48k_to_16k,
)


def test_downmix_stereo_to_mono_averages_channels():
    stereo = np.array([10, 20, 30, 40, -10, 10], dtype=np.int16)  # 3 frames
    mono = downmix_stereo_to_mono(stereo)
    assert np.allclose(mono, [15.0, 35.0, 0.0])


def test_resample_48k_to_16k_downsamples_by_three():
    mono = np.arange(9, dtype=np.float64)  # 9 samples -> 3 samples
    out = resample_48k_to_16k(mono)
    assert np.allclose(out, [1.0, 4.0, 7.0])  # 每 3 點平均


def test_resample_handles_short_input():
    assert len(resample_48k_to_16k(np.array([1.0, 2.0]))) == 0


def test_estimate_delay_samples_recovers_known_shift():
    """ref 是比 mic 更長、起點更早的 buffer；mic 是 ref 裡從 true_delay 開始的一段。
    estimate_delay_samples 要能找出 mic 對應 ref 的哪個起點。"""
    n_ref = 4300
    t = np.arange(n_ref) / 16000.0
    ref = np.sin(2 * np.pi * 300.0 * t) + 0.5 * np.sin(2 * np.pi * 900.0 * t)
    true_delay = 137
    mic = ref[true_delay : true_delay + 4000]

    estimated = estimate_delay_samples(mic, ref, max_delay=300)
    assert abs(estimated - true_delay) <= 2


def test_puck_aec_processor_cancels_delayed_resampled_echo():
    """端到端：48kHz stereo reference → 延遲 → 縮混/降採到 16kHz 當 mic 收到的回音。"""
    n_ref = 48000 * 2  # 2 秒 reference（要夠長蓋過延遲搜尋範圍+mic段）
    t_ref = np.arange(n_ref) / 48000.0
    left = 8000 * np.sin(2 * np.pi * 250.0 * t_ref)
    right = 8000 * np.sin(2 * np.pi * 900.0 * t_ref + 0.4)
    ref_stereo = np.empty(n_ref * 2, dtype=np.int16)
    ref_stereo[0::2] = left.astype(np.int16)
    ref_stereo[1::2] = right.astype(np.int16)

    ref_mono_16k = resample_48k_to_16k(downmix_stereo_to_mono(ref_stereo))
    true_delay = 80  # samples @16kHz ≈ 5ms
    echo = ref_mono_16k[true_delay:] * 0.6

    mic_len = 6000
    near_end = 20.0 * np.sin(2 * np.pi * 5.0 * np.arange(mic_len) / 16000.0)
    mic = echo[:mic_len] + near_end

    processor = PuckAecProcessor(filter_length=32, mu=0.5, max_delay_ms=200.0)
    # reference 要涵蓋 mic 這段 + 延遲搜尋餘裕，從一開始餵完整 2 秒
    residual = processor.process(mic, ref_stereo)

    from device.aec_nlms import erle_db

    late_erle = erle_db(mic[-1000:], residual[-1000:])
    assert late_erle > 10.0, f"AEC 應該對延遲+降採後的回音仍有明顯壓制，實測 {late_erle:.1f}dB"


def test_puck_aec_processor_no_reference_returns_mic_unchanged():
    processor = PuckAecProcessor()
    mic = np.array([1.0, 2.0, 3.0])
    out = processor.process(mic, np.zeros(0, dtype=np.int16))
    assert np.allclose(out, mic)
