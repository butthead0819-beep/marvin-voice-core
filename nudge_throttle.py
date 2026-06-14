"""通用使用者提醒節流器（純核心）。

任何功能要對某使用者發提醒都走這裡，共用同一套抑制原則：
- 短窗內累積 ≥min_attempts 個有效訊號才觸發（濾掉單次噪音 → 窄訊號）
- 每 (category, speaker) 每段語音 session 最多觸發一次（防洗版）
- session 邊界由呼叫端在使用者 (重)進語音時呼叫 reset_speaker

呼叫端只在「確有有效訊號」時呼叫 signal()；節流器不懂各功能語意，
喚醒詞匹配 / 音量判定等由呼叫端自行判好。純核心無 regex / 無 I/O。

首個 consumer = 環境噪音喚醒提醒（category="noise"，對應 2026-06-13 showay
incident：放音樂 + 背景吵 → 喚醒詞被 STT 糊掉 + Echo Guard 全擋 → 整晚喚不醒）。
"""
from __future__ import annotations


class NudgeThrottle:
    def __init__(self, window_s: float = 60.0, min_attempts: int = 2):
        self.default_window_s = window_s
        self.default_min_attempts = min_attempts
        self._attempts: dict[tuple[str, str], list[float]] = {}
        self._fired: set[tuple[str, str]] = set()

    def signal(
        self,
        category: str,
        speaker: str,
        now: float,
        *,
        window_s: float | None = None,
        min_attempts: int | None = None,
    ) -> bool:
        """記錄一個 category 的有效訊號；回傳 True = 此刻該對該 speaker 發此類提醒。

        本 session 此 (category, speaker) 已觸發過、或窗內未達次數門檻 → False。
        window_s / min_attempts 可單次覆寫（不同功能要求不同靈敏度）。
        """
        key = (category, speaker)
        if key in self._fired:
            return False
        w = self.default_window_s if window_s is None else window_s
        n = self.default_min_attempts if min_attempts is None else min_attempts
        window = [t for t in self._attempts.get(key, []) if now - t < w]
        window.append(now)
        self._attempts[key] = window
        if len(window) >= n:
            self._fired.add(key)
            self._attempts.pop(key, None)
            return True
        return False

    def reset_speaker(self, speaker: str) -> None:
        """speaker (重)進語音 = 新 session → 清掉該人所有類別，重新武裝。"""
        self._fired = {k for k in self._fired if k[1] != speaker}
        self._attempts = {k: v for k, v in self._attempts.items() if k[1] != speaker}
