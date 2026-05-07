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

        return {
            "wps": round(wps, 2),
            "char_count": char_count,
            "energy_variance": round(variance, 2),
            "physical_duration": round(physical_duration, 2),
            "sample_count": len(samples)
        }

    def clear(self, user_id: int):
        """清除指定使用者的緩衝區"""
        if user_id in self.rms_history:
            self.rms_history.pop(user_id)
