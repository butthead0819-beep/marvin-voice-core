import struct
import pytest
from marvin_voice_core.audio_utils import calculate_rms, apply_gain


def _make_pcm(samples: list[int]) -> bytes:
    return struct.pack(f"{len(samples)}h", *samples)


class TestCalculateRms:
    def test_silent_returns_zero(self):
        pcm = _make_pcm([0] * 100)
        assert calculate_rms(pcm) == 0

    def test_empty_bytes_returns_zero(self):
        assert calculate_rms(b"") == 0

    def test_constant_signal(self):
        # RMS of a constant signal equals that constant
        pcm = _make_pcm([1000] * 100)
        result = calculate_rms(pcm)
        assert abs(result - 1000) <= 1  # rounding tolerance

    def test_sine_approximation(self):
        # Rough sine: peak 10000 → RMS ≈ 7071
        import math
        samples = [int(10000 * math.sin(2 * math.pi * i / 100)) for i in range(200)]
        result = calculate_rms(_make_pcm(samples))
        assert 6900 < result < 7200


class TestApplyGain:
    def test_empty_bytes_unchanged(self):
        assert apply_gain(b"") == b""

    def test_gain_one_unchanged(self):
        pcm = _make_pcm([1000, -1000, 500, -500])
        assert apply_gain(pcm, gain=1.0) == pcm

    def test_doubles_amplitude(self):
        pcm = _make_pcm([1000, -1000])
        result = apply_gain(pcm, gain=2.0)
        samples = list(struct.unpack("2h", result))
        assert samples == [2000, -2000]

    def test_clips_at_int16_max(self):
        pcm = _make_pcm([30000])
        result = apply_gain(pcm, gain=2.0)
        (sample,) = struct.unpack("1h", result)
        assert sample == 32767

    def test_clips_at_int16_min(self):
        pcm = _make_pcm([-30000])
        result = apply_gain(pcm, gain=2.0)
        (sample,) = struct.unpack("1h", result)
        assert sample == -32768

    def test_output_length_matches_input(self):
        pcm = _make_pcm([100, 200, 300, 400, 500])
        assert len(apply_gain(pcm, gain=1.8)) == len(pcm)
