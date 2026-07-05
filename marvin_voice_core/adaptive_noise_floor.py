"""單串流自適應噪音地板（Adaptive Noise Floor）。

抽自 marvin_voice_core/sink.py 的 per-user 演算法（RealtimeVADSink 用滾動 75-packet
視窗學背景 RMS），做成單串流版供 LocalMicSink 複用——本機麥克風的底噪必須「取樣」
而非寫死單一門檻（真房間底噪 ≫ Discord 乾淨數位音訊，且會隨環境變）。

演算法（對齊 CLAUDE.md〈自適應噪音地板〉規範）：
  - 滾動視窗（WINDOW=75 packets）維護 sum_x / sum_x2 增量算平均與變異數
  - 僅在背景平穩（variance < VARIANCE_STABLE）時把地板更新成視窗均值
    （避免把人聲高變異學成背景）
  - Deadlock recovery：輸入驟降到地板 DEADLOCK_RATIO 以下 → 清空視窗、地板重置
    （背景由吵轉靜時不卡在舊高地板）
  - 動態閾值 = max(靜態最低值, noise_floor + DELTA, noise_floor × SNR_MULT)

常數與 sink.py 逐一對齊；此類為純運算、無 I/O，可離線單元測。
"""
from __future__ import annotations

from collections import deque

# 與 marvin_voice_core/sink.py 的 per-user 實作逐一對齊（勿各自漂移）。
_WINDOW = 75
_VARIANCE_STABLE = 1600.0
_DELTA = 100.0
_SNR_MULT = 1.5
_FLOOR_INIT = 50.0
_FLOOR_MIN = 10.0
_DEADLOCK_RATIO = 0.4


class AdaptiveNoiseFloor:
    WINDOW = _WINDOW
    VARIANCE_STABLE = _VARIANCE_STABLE
    DELTA = _DELTA
    SNR_MULT = _SNR_MULT
    FLOOR_INIT = _FLOOR_INIT
    FLOOR_MIN = _FLOOR_MIN
    DEADLOCK_RATIO = _DEADLOCK_RATIO

    def __init__(self, static_floor: float) -> None:
        self._static_floor = float(static_floor)
        self._floor = self.FLOOR_INIT
        self._history: deque[float] = deque(maxlen=self.WINDOW)
        self._sum_x = 0.0
        self._sum_x2 = 0.0

    @property
    def noise_floor(self) -> float:
        return self._floor

    def update(self, rms: float) -> float:
        """吃一個 packet 的 RMS、更新地板，回傳當前 active threshold。

        呼叫方判 speech 用 `rms > update(rms)`（current packet 已納入視窗，
        與 sink.py 的比較順序一致）。
        """
        # Deadlock recovery：背景驟降 → 清空重置，別卡在舊高地板
        if rms < self._floor * self.DEADLOCK_RATIO:
            self._history.clear()
            self._sum_x = 0.0
            self._sum_x2 = 0.0
            self._floor = max(self.FLOOR_MIN, float(rms))

        # 滾動視窗增量維護
        if len(self._history) == self.WINDOW:
            old = self._history.popleft()
            self._sum_x -= old
            self._sum_x2 -= old ** 2
        self._history.append(rms)
        self._sum_x += rms
        self._sum_x2 += rms ** 2

        # 視窗滿且背景平穩才更新地板
        count = len(self._history)
        if count == self.WINDOW:
            mean_rms = self._sum_x / count
            variance = max(0.0, (self._sum_x2 - (self._sum_x ** 2) / count) / count)
            if variance < self.VARIANCE_STABLE:
                self._floor = float(mean_rms)

        dynamic = max(self._static_floor, self._floor + self.DELTA)
        snr = self._floor * self.SNR_MULT
        return max(dynamic, snr)
