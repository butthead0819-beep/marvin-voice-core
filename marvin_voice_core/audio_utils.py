import wave
import os
import numpy as np

def calculate_rms(pcm_bytes, width=2):
    """計算 PCM 資料的 RMS 音量"""
    try:
        if not pcm_bytes:
            return 0
        dtype = np.int16 if width == 2 else np.int8
        arr = np.frombuffer(pcm_bytes, dtype=dtype).astype(np.float32)
        return int(np.sqrt(np.mean(arr ** 2)))
    except Exception:
        return 0

def apply_gain(pcm_bytes, gain=1.8, width=2):
    """對 PCM 資料套用增益"""
    try:
        if not pcm_bytes:
            return pcm_bytes
        dtype = np.int16 if width == 2 else np.int8
        info = np.iinfo(dtype)
        arr = np.frombuffer(pcm_bytes, dtype=dtype).astype(np.float32)
        gained = np.clip(arr * gain, info.min, info.max).astype(dtype)
        return gained.tobytes()
    except Exception:
        return pcm_bytes

# ── 響度正規化（目標 RMS 制，2026-06-13）────────────────────────────────────
# 取代固定 1.8x Golden Ear：實測 RMS 分布 p10=294/中位 2751，小聲講者
# （weakgogo 空白率 2.2% = 4 倍於最佳者）固定 1.8x 救不起來。

def normalize_rms(pcm_bytes: bytes, *, target_rms: float = 2800.0,
                  max_gain: float = 6.0, min_rms: float = 100.0,
                  peak_ceiling: int = 30000) -> bytes:
    """int16 PCM 響度正規化：把 RMS 拉向 target，永不衰減、永不削波。

    - rms < min_rms → 雜訊，原樣返回（放大垃圾害 STT）
    - gain = clamp(target/rms, 1.0, max_gain)，再受 peak_ceiling/peak 保護
      （寧可 boost 不足，不做 clip 失真）
    - gain ≈ 1 時原樣返回（省一次重編碼）
    """
    if not pcm_bytes:
        return pcm_bytes
    arr = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    rms = float(np.sqrt(np.mean(arr ** 2)))
    if rms < min_rms or rms >= target_rms:
        return pcm_bytes
    gain = min(target_rms / rms, max_gain)
    peak = float(np.abs(arr).max())
    if peak > 0:
        gain = min(gain, peak_ceiling / peak)
    if gain <= 1.01:
        return pcm_bytes
    return (arr * gain).clip(-32768, 32767).astype(np.int16).tobytes()


# ── 48k stereo → 16k mono 抗混疊降頻（2026-06-13）──────────────────────────
# 舊版 `mean(axis=1)[::3]` 裸抽取無低通，>8kHz 能量摺疊回語音帶。
# windowed-sinc FIR（純 numpy，不引 scipy）：cutoff 7.2kHz、63 taps Hamming，
# 一次 utterance（~4s/192k samples）約數 ms，可留在 event loop。

_AA_TAPS_CACHE = None


def _antialias_taps(numtaps: int = 63, cutoff_norm: float = 0.15) -> np.ndarray:
    """windowed-sinc 低通 FIR 係數。cutoff_norm 以 48kHz 取樣率正規化（0.15 ≈ 7.2kHz）。"""
    global _AA_TAPS_CACHE
    if _AA_TAPS_CACHE is None:
        n = np.arange(numtaps) - (numtaps - 1) / 2
        taps = 2 * cutoff_norm * np.sinc(2 * cutoff_norm * n)
        taps *= np.hamming(numtaps)
        _AA_TAPS_CACHE = (taps / taps.sum()).astype(np.float64)
    return _AA_TAPS_CACHE


def pcm48k_stereo_to_16k_mono(pcm_bytes: bytes) -> np.ndarray:
    """48kHz stereo int16 PCM → 16kHz mono float32（[-1,1]），先抗混疊低通再 3:1 抽取。

    輸出契約與舊裸抽取版相同（dtype/range/長度 n//3），下游 Whisper/雅婷/Gemini
    lane 零改動。空輸入回空 array。
    """
    if not pcm_bytes:
        return np.array([], dtype=np.float32)
    arr = np.frombuffer(pcm_bytes, dtype=np.int16)
    arr = arr[: len(arr) // 2 * 2].reshape(-1, 2)
    mono = arr.mean(axis=1) / 32768.0
    filtered = np.convolve(mono, _antialias_taps(), mode="same")
    return filtered[::3].astype(np.float32)


def save_wav(pcm_bytes, file_path, channels=2, width=2, sample_rate=48000):
    """將 PCM 資料存為 WAV 檔案"""
    with wave.open(file_path, 'wb') as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(width)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_bytes)
    return os.path.abspath(file_path)
