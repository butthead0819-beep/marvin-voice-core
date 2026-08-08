"""
puck_aec.py — 車 puck INMP441 麥克風 × puck_mixer 回音消除整合層。

puck_mixer.PuckMixer 已經把送進喇叭前的 PCM（48kHz stereo）存進
recent_reference() 這個 ring buffer（見 puck_mixer.py）。這裡負責：
1. 把 reference 從 48kHz stereo 轉成跟 INMP441 麥克風一致的 16kHz mono
2. 用互相關（cross-correlation）估計 mic/reference 之間的延遲（buffer+空氣傳播延遲）
3. 對齊後丟給 aec_nlms.NLMSEchoCanceller 做實際的回音消除

跟家用 Pi3B 那條路不同：這裡的 reference 是 puck_mixer 自己 process 內
的 PCM buffer，bit-exact、不用另外接 wrapper tap。
"""
import numpy as np

from device.aec_nlms import NLMSEchoCanceller

REF_RATE = 48000
MIC_RATE = 16000
RESAMPLE_RATIO = REF_RATE // MIC_RATE  # 3


def downmix_stereo_to_mono(stereo_interleaved: np.ndarray) -> np.ndarray:
    """stereo interleaved int16/float → mono float64（左右平均）。"""
    stereo = np.asarray(stereo_interleaved, dtype=np.float64).reshape(-1, 2)
    return stereo.mean(axis=1)


def resample_48k_to_16k(mono: np.ndarray) -> np.ndarray:
    """簡單區塊平均降採樣（每 3 點平均），無 scipy 依賴，兼具粗略防疊頻。"""
    n = (len(mono) // RESAMPLE_RATIO) * RESAMPLE_RATIO
    if n == 0:
        return np.zeros(0, dtype=np.float64)
    trimmed = np.asarray(mono, dtype=np.float64)[:n]
    return trimmed.reshape(-1, RESAMPLE_RATIO).mean(axis=1)


def estimate_delay_samples(mic: np.ndarray, ref: np.ndarray, max_delay: int) -> int:
    """回傳 ref 要往前跳過幾個 sample 才會對齊 mic 裡量到的回音（0 <= delay < max_delay）。
    要求 ref 比 mic 長至少 max_delay，否則能量到的候選延遲範圍會被截短。"""
    mic_c = np.asarray(mic, dtype=np.float64)
    ref_c = np.asarray(ref, dtype=np.float64)
    mic_c = mic_c - mic_c.mean()
    ref_c = ref_c - ref_c.mean()
    best_lag = 0
    best_score = -np.inf
    upper = min(max_delay, max(1, len(ref_c) - 1))
    for lag in range(upper):
        n = min(len(mic_c), len(ref_c) - lag)
        if n <= 0:
            break
        score = float(np.dot(mic_c[:n], ref_c[lag : lag + n]))
        if score > best_score:
            best_score = score
            best_lag = lag
    return best_lag


class PuckAecProcessor:
    """整合 resample + 延遲對齊 + NLMS 的一站式回音消除器。"""

    def __init__(self, filter_length: int = 256, mu: float = 0.5, max_delay_ms: float = 200.0):
        self.canceller = NLMSEchoCanceller(filter_length=filter_length, mu=mu)
        self.max_delay_samples = int(MIC_RATE * max_delay_ms / 1000.0)

    def process(self, mic_16k_mono: np.ndarray, ref_48k_stereo: np.ndarray) -> np.ndarray:
        """mic_16k_mono: INMP441 錄到的一段麥克風訊號。
        ref_48k_stereo: 從 PuckMixer.recent_reference() 拿到、涵蓋同一段時間+
        max_delay 餘裕的 reference（必須比 mic 段長，才能容納延遲搜尋範圍）。
        回傳跟 mic_16k_mono 等長、消完回音的殘差。"""
        mic = np.asarray(mic_16k_mono, dtype=np.float64)
        ref_mono_16k = resample_48k_to_16k(downmix_stereo_to_mono(ref_48k_stereo))
        if len(ref_mono_16k) == 0:
            return mic.copy()
        delay = estimate_delay_samples(mic, ref_mono_16k, self.max_delay_samples)
        aligned_ref = ref_mono_16k[delay : delay + len(mic)]
        if len(aligned_ref) < len(mic):
            aligned_ref = np.pad(aligned_ref, (0, len(mic) - len(aligned_ref)))
        return self.canceller.process_block(mic, aligned_ref)
