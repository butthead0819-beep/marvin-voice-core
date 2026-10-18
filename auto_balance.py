"""Active Music Balance：依聊天室熱度自動調語音/音樂平衡（2026-10-18 使用者定）。
很熱 90/10、偶爾幾句 70/30、60 秒沒人講話 50/50。純函式 + 小狀態物件，無 IO。"""
from __future__ import annotations

from dj_heat import _human_entries, heat_snapshot

AUTO_BALANCE_QUIET_S = 60.0          # 這麼久沒真人講話 → 回 50/50
AUTO_BALANCE_MANUAL_HOLD_S = 600.0   # 手動按過平衡後，自動暫停這麼久
AUTO_BALANCE_RAMP_S = 3.0            # 自動調整的漸變時間
AUTO_BALANCE_CONFIRM_TICKS = 2       # 同一新目標連續幾次才套用（防邊界抖動）

BALANCE_QUIET = 0.50
BALANCE_SOME = 0.70
BALANCE_HOT = 0.90


def target_balance(entries, n_online: int, now: float) -> float | None:
    if n_online <= 0:
        return None
    if not _human_entries(entries, now, AUTO_BALANCE_QUIET_S):
        return BALANCE_QUIET
    _count, _threshold, hot = heat_snapshot(entries, n_online, now)
    return BALANCE_HOT if hot else BALANCE_SOME


class AutoBalanceState:
    """遲滯：與現值不同的同一目標連續 confirm_ticks 次才回傳要套用的值。"""

    def __init__(self, confirm_ticks: int = AUTO_BALANCE_CONFIRM_TICKS) -> None:
        self._confirm = confirm_ticks
        self._pending: float | None = None
        self._count = 0

    def update(self, target: float | None, actual: float) -> float | None:
        if target is None or abs(target - actual) < 1e-6:
            self._pending, self._count = None, 0
            return None
        if self._pending is not None and abs(self._pending - target) < 1e-6:
            self._count += 1
        else:
            self._pending, self._count = target, 1
        if self._count >= self._confirm:
            self._pending, self._count = None, 0
            return target
        return None


def manual_hold_active(manual_at: float, now: float) -> bool:
    return now - manual_at < AUTO_BALANCE_MANUAL_HOLD_S
