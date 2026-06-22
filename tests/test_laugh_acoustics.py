"""笑聲節律啟發式：規律爆發包絡 → looks_like_laugh。"""
import io
import math
import wave

from laugh_acoustics import (
    rms_envelope, rhythm_features, looks_like_laugh,
    wav_bytes_to_mono, rhythm_from_wav_bytes)


def _burst_envelope(rate_hz, seconds, frame_rate_hz=50.0):
    """造規律爆發包絡：每個週期前半高、後半低（模擬哈-哈-哈）。"""
    period = int(frame_rate_hz / rate_hz)
    env = []
    for _ in range(int(seconds * frame_rate_hz / period)):
        env += [100.0] * (period // 2) + [5.0] * (period - period // 2)
    return env


def test_regular_bursts_in_band_detected_as_laugh():
    env = _burst_envelope(rate_hz=5.0, seconds=1.5)   # 5 Hz、規律 → 笑帶
    f = rhythm_features(env, frame_rate_hz=50.0)
    assert 3.0 <= f["peaks_per_sec"] <= 9.0
    assert f["regularity"] >= 0.5
    assert looks_like_laugh(f) is True


def test_flat_envelope_not_laugh():
    f = rhythm_features([50.0] * 75, frame_rate_hz=50.0)
    assert f["bursts"] == 0
    assert looks_like_laugh(f) is False


def test_irregular_envelope_low_regularity_not_laugh():
    # 爆發間隔明顯不等（模擬講話）→ regularity 低
    env = [5.0] * 60
    for i in (1, 3, 21, 23, 51):   # 上行間隔 2,18,2,28 → 高變異
        env[i] = 100.0
    f = rhythm_features(env, frame_rate_hz=50.0)
    assert f["regularity"] < 0.5
    assert looks_like_laugh(f) is False


def test_too_slow_bursts_out_of_band_not_laugh():
    env = _burst_envelope(rate_hz=1.0, seconds=3.0)   # 1 Hz、太慢 → 非笑
    f = rhythm_features(env, frame_rate_hz=50.0)
    assert f["peaks_per_sec"] < 3.0
    assert looks_like_laugh(f) is False


def test_rms_envelope_frames_energy():
    samples = [0, 0, 100, 100, 0, 0, 200, 200]
    env = rms_envelope(samples, frame_len=2)
    assert env == [0.0, 100.0, 0.0, 200.0]


def _wav(samples, sr=48000, ch=2):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(sr)
        import array
        if ch == 2:
            inter = array.array("h")
            for s in samples:
                inter.append(s); inter.append(s)
            w.writeframes(inter.tobytes())
        else:
            w.writeframes(array.array("h", samples).tobytes())
    return buf.getvalue()


def test_wav_bytes_to_mono_averages_stereo():
    mono, sr = wav_bytes_to_mono(_wav([10, 20, 30], sr=16000, ch=2))
    assert sr == 16000 and mono == [10, 20, 30]


def test_rhythm_from_wav_bytes_detects_laugh_burst():
    # 5 Hz 規律爆發、48k：每週期 9600 樣本，前半振幅高後半低
    sr = 48000
    period = sr // 5
    samples = []
    for _ in range(8):  # ~1.6s
        samples += [8000] * (period // 2) + [50] * (period - period // 2)
    f = rhythm_from_wav_bytes(_wav(samples, sr=sr, ch=1))
    assert looks_like_laugh(f) is True


def test_rhythm_from_wav_bytes_empty_safe():
    f = rhythm_from_wav_bytes(b"")
    assert f["bursts"] == 0 and looks_like_laugh(f) is False
