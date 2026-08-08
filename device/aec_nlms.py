"""
aec_nlms.py — 家用 Pi3B AirPlay/wyoming-satellite 軟體 AEC 核心演算法（PoC）。

用途：喚醒詞偵測前，把「Marvin 自己放出去的音樂/TTS」從麥克風訊號中減掉。
跟先前失敗的「錄放出來的聲音當 reference」不同，這裡假設呼叫端已經把
**送進 DigiAMP 前的原始 PCM**（bit-exact，非聲學錄製）當 reference 餵進來
（wyoming-satellite `--snd-command` wrapper 要負責 tap 這份訊號 + resample
到跟麥克風相同取樣率，這個模組本身不處理取樣率轉換）。

演算法：NLMS（Normalized Least Mean Squares）自適應濾波器，逐點更新權重，
把 reference 訊號用適應性 FIR 濾波器估計出「reference 造成的麥克風端回音」，
從麥克風訊號中減掉。純 numpy、無需編譯，Pi3B 上跑一定動，但逐點迴圈在
16kHz 即時串流下效能未驗證（見 README 底部備註），先驗證演算法本身正確。
"""
import numpy as np


class NLMSEchoCanceller:
    """單聲道 NLMS 回音消除器。呼叫端保證 mic/ref 已對齊取樣率與時間延遲。"""

    def __init__(self, filter_length: int = 256, mu: float = 0.5, eps: float = 1e-6):
        if filter_length <= 0:
            raise ValueError("filter_length 必須 > 0")
        if not (0 < mu <= 1.0):
            raise ValueError("mu 建議落在 (0, 1]")
        self.filter_length = filter_length
        self.mu = mu
        self.eps = eps
        self.weights = np.zeros(filter_length, dtype=np.float64)
        self._ref_history = np.zeros(filter_length, dtype=np.float64)

    def reset(self):
        self.weights[:] = 0.0
        self._ref_history[:] = 0.0

    def process_sample(self, mic_sample: float, ref_sample: float) -> float:
        """回傳消除回音後的殘差（= 乾淨的麥克風訊號的一個取樣點）。"""
        self._ref_history[1:] = self._ref_history[:-1]
        self._ref_history[0] = ref_sample
        estimated_echo = float(np.dot(self.weights, self._ref_history))
        error = mic_sample - estimated_echo
        norm = float(np.dot(self._ref_history, self._ref_history)) + self.eps
        self.weights += (self.mu / norm) * error * self._ref_history
        return error

    def process_block(self, mic_block: np.ndarray, ref_block: np.ndarray) -> np.ndarray:
        """逐點跑 process_sample，回傳同長度的殘差陣列（float64）。"""
        if len(mic_block) != len(ref_block):
            raise ValueError("mic_block 與 ref_block 長度必須一致（呼叫端須先對齊）")
        out = np.empty(len(mic_block), dtype=np.float64)
        for i in range(len(mic_block)):
            out[i] = self.process_sample(mic_block[i], ref_block[i])
        return out


def erle_db(mic_block: np.ndarray, residual_block: np.ndarray) -> float:
    """Echo Return Loss Enhancement，估算回音被壓低了幾 dB（越大越好）。"""
    mic_power = float(np.mean(np.square(mic_block))) + 1e-12
    residual_power = float(np.mean(np.square(residual_block))) + 1e-12
    return 10.0 * np.log10(mic_power / residual_power)
