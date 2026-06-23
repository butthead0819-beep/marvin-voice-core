"""DecryptHealthMonitor：偵測接收端「收到封包卻持續解不開」的 secret_key desync 風暴。

背景（2026-06-23 incident）：網路抖動 → discord.py 快速 RESUME 語音 session → 沿用舊
secret_key，但 Discord 在空檔換了 key → 接收封包永遠解不開（CryptoError 風暴）。KeySync
補丁重抓 `voice_client.secret_key` 重試，但那把 key 本身就是舊的、重讀無用；而這層傳輸層
CryptoError 被 KeySync drop 掉、Sentinel（只數 DAVE 層）看不到 → 炸 40 分鐘升級永不觸發。
只有一次**完整重連**（非 RESUME）拿到新 key 才修得好。

本模組是純邏輯（now 由 caller 傳入、無時鐘無 IO），偵測「持續零成功解密」→ 通知 caller
觸發完整重連自癒。CryptoError 要有封包才會出現 → 沒人講話不誤判（天然 gate）。
"""
from __future__ import annotations


class DecryptHealthMonitor:
    """收到封包卻持續解不開的偵測器。

    record_success / record_failure 餵入每次解密結果（now=time.time()）；
    should_escalate(now) 在「連續 ≥min_failures 次失敗且跨度 ≥sustained_s 秒、期間零成功」
    時回 True 一次（升級完整重連），之後等下一次 record_success 才能再升級（不 spam）。

    - min_failures：確認封包真的在穩定流進（不是零星雜散封包）。
    - sustained_s：撐過這段時間仍零解密 → 不是 KeySync 救得回的瞬間抖動、是真 desync。
    """

    def __init__(self, sustained_s: float = 8.0, min_failures: int = 10):
        self.sustained_s = sustained_s
        self.min_failures = min_failures
        self._fail_streak = 0
        self._streak_start = 0.0
        self._escalated = False

    def record_success(self, now: float) -> None:
        """成功解密（key 同步正常）→ 清 streak、解除升級閂（恢復後可再升級）。"""
        self._fail_streak = 0
        self._streak_start = 0.0
        self._escalated = False

    def record_failure(self, now: float) -> None:
        """KeySync 重抓 key 後仍 CryptoError（key 本身壞）→ 累計連續失敗。"""
        if self._fail_streak == 0:
            self._streak_start = now
        self._fail_streak += 1

    def should_escalate(self, now: float) -> bool:
        """是否該升級完整重連。達標時回 True 一次後上閂，等 record_success 才會再放行。"""
        if self._escalated:
            return False
        if self._fail_streak < self.min_failures:
            return False
        if now - self._streak_start < self.sustained_s:
            return False
        self._escalated = True
        return True
