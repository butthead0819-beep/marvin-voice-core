import numpy as np
import pytest

from device.aec_nlms import NLMSEchoCanceller, erle_db


def _make_reference(n_samples: int) -> np.ndarray:
    """決定性、頻譜豐富的類音樂訊號（多個固定頻率疊加），避免用真隨機。"""
    t = np.arange(n_samples) / 16000.0
    ref = (
        0.5 * np.sin(2 * np.pi * 220.0 * t)
        + 0.3 * np.sin(2 * np.pi * 880.0 * t)
        + 0.2 * np.sin(2 * np.pi * 1760.0 * t + 0.3)
    )
    return ref


def _true_echo_path(length: int = 32) -> np.ndarray:
    """固定、已知的模擬回音路徑（等同「送進 DAC 前的訊號」bit-exact 情境）。"""
    h = 0.7 ** np.arange(length)
    h *= np.cos(np.linspace(0, 3.0, length))
    return h


def test_erle_improves_substantially_with_bitexact_reference():
    n_samples = 8000
    ref = _make_reference(n_samples)
    h = _true_echo_path(32)
    echo = np.convolve(ref, h)[:n_samples]
    near_end_floor = 0.01 * np.sin(2 * np.pi * 5.0 * np.arange(n_samples) / 16000.0)
    mic = echo + near_end_floor

    canceller = NLMSEchoCanceller(filter_length=64, mu=0.5)
    residual = canceller.process_block(mic, ref)

    early_erle = erle_db(mic[:50], residual[:50])
    late_erle = erle_db(mic[-1000:], residual[-1000:])

    assert late_erle > early_erle, (
        f"NLMS 應該隨時間收斂、提升 ERLE: early={early_erle:.1f}dB late={late_erle:.1f}dB"
    )
    assert late_erle > 20.0, f"收斂後 ERLE 應該 > 20dB（回音壓低超過99%），實測 {late_erle:.1f}dB"


def test_uncorrelated_reference_does_not_blow_up_or_amplify():
    """reference 跟麥克風訊號無關時（例如尚未播放任何東西），濾波器不該發散或放大雜訊。"""
    n_samples = 4000
    rng = np.random.default_rng(42)
    ref = _make_reference(n_samples)
    mic = 0.05 * rng.standard_normal(n_samples)  # 跟 ref 無關的近端訊號

    canceller = NLMSEchoCanceller(filter_length=64, mu=0.5)
    residual = canceller.process_block(mic, ref)

    assert np.all(np.isfinite(residual))
    residual_power = float(np.mean(np.square(residual[-500:])))
    mic_power = float(np.mean(np.square(mic[-500:])))
    assert residual_power < mic_power * 3.0, "無關 reference 不該讓輸出功率暴增（濾波器發散）"


def test_process_block_rejects_mismatched_lengths():
    canceller = NLMSEchoCanceller(filter_length=16)
    with pytest.raises(ValueError):
        canceller.process_block(np.zeros(10), np.zeros(5))


def test_invalid_params_rejected():
    with pytest.raises(ValueError):
        NLMSEchoCanceller(filter_length=0)
    with pytest.raises(ValueError):
        NLMSEchoCanceller(filter_length=16, mu=0.0)
    with pytest.raises(ValueError):
        NLMSEchoCanceller(filter_length=16, mu=1.5)
