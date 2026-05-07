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

def save_wav(pcm_bytes, file_path, channels=2, width=2, sample_rate=48000):
    """將 PCM 資料存為 WAV 檔案"""
    with wave.open(file_path, 'wb') as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(width)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_bytes)
    return os.path.abspath(file_path)
