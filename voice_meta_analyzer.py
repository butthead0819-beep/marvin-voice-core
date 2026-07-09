import time
import statistics
from collections import deque

class VoiceMetaAnalyzer:
    """
    [Operation Prosody Perception]
    語音元數據分析器：負責分析語速 (WPS)、能量波動 (Energy Variance) 與行為模式。
    """
    def __init__(self, max_samples=1000):
        self.rms_history = {} # user_id -> deque of RMS values
        self.max_samples = max_samples
        self.rms_baseline: dict = {}  # user_id -> 每人 EMA 基準（alpha=0.2 PROVISIONAL）
        self.last_softness: float = 0.0  # 最近一次發聲的軟度 [0,1]；1-on-1 單值

    def add_rms(self, user_id: int, rms: float):
        """將每 20ms 的 RMS 能量值存入雙端隊列 (輕量級採樣)"""
        if user_id not in self.rms_history:
            self.rms_history[user_id] = deque(maxlen=self.max_samples)
        self.rms_history[user_id].append(rms)

    def calculate_prosody(self, user_id: int, text: str = None, physical_duration: float = 0.0) -> dict:
        """
        計算該段音訊的韻律元數據。
        WPS: Words Per Second (字數 / 物理時長)
        Energy Variance: RMS 標準差 (代表抑揚頓挫)
        """
        if user_id not in self.rms_history:
            return {}

        samples = list(self.rms_history.pop(user_id))
        if not samples and physical_duration <= 0:
            return {}

        # 1. 語速偵測 (WPS) - 只有在提供 text 時才計算
        wps = 0.0
        char_count = 0
        if text and text != "placeholder":
            char_count = len(text.replace(" ", ""))
            wps = char_count / physical_duration if physical_duration > 0 else 0

        # 2. 能量波動 (Standard Deviation of RMS)
        variance = 0.0
        if len(samples) > 1:
            try:
                variance = statistics.stdev(samples)
            except Exception:
                variance = 0.0

        mean_rms = round(statistics.mean(samples), 2) if samples else 0.0

        # 更新每人 EMA 基準 + 計算軟度（僅在有採樣時；空採樣路徑保留舊值）
        if samples:
            # alpha=0.2 PROVISIONAL（可 live-tune）
            prev = self.rms_baseline.get(user_id)
            baseline = mean_rms if prev is None else 0.2 * mean_rms + 0.8 * prev
            self.rms_baseline[user_id] = baseline
            # 比基準軟 → >0；等於或大於基準 → 夾到 0
            self.last_softness = (
                min(1.0, max(0.0, (baseline - mean_rms) / baseline))
                if baseline > 0 else 0.0
            )

        return {
            "wps": round(wps, 2),
            "char_count": char_count,
            "energy_variance": round(variance, 2),
            "physical_duration": round(physical_duration, 2),
            "sample_count": len(samples),
            "mean_rms": mean_rms,
        }

    def clear(self, user_id: int):
        """清除指定使用者的緩衝區"""
        if user_id in self.rms_history:
            self.rms_history.pop(user_id)
