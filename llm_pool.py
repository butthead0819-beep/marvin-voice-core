"""CooldownAwarePool — 把數個 free-tier LLM endpoint 當成一個算力池。

問題（2026-05-21 prod，單人測試就撞 Groq 500K TPD 上限）：每個 caller 各自寫
try/except + retry，429 後**每次呼叫還是先撞同一個 endpoint** 才 fallback，浪費
round-trip + 拖慢 pipeline。stt_cleaner 已有 per-engine cooldown（proven pattern），
但 gemini_router_llm 完全沒 cooldown 記憶（2026-05-20 一晚 43 次 Groq 429）。

解法（project_llm_tier_wrapper.md，Jack 拍板）：抽成共用 pool。
- 429 → mark_429 記 cooldown_until，冷卻期 next_available **直接跳過不撞**
- TPM 接近上限（budget*headroom）也跳，留 buffer
- next_available() 是唯一入口——caller 不再自己寫 try/except chain，pool 自動分流
  到下一個有 quota 的 endpoint；全滿回 None（caller 兜底）

本模組只管**狀態 + 選 endpoint**（純邏輯、可單測）；實際 LLM 呼叫由 caller 拿
回傳的 endpoint 自己做，做完用 record_usage / mark_429 回報。
"""
from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

_RETRY_AFTER_DEFAULT = 30.0


def parse_retry_after(err: str, default: float = _RETRY_AFTER_DEFAULT) -> float:
    """從 429 訊息抽 retry-after 秒數。

    支援 Groq 形式 'Please try again in 4m18.8544s' / 'in 30s' / 'in 1m'。
    抽不到 → default（保守冷卻，不要 0 否則等於沒冷卻）。
    """
    if not err:
        return default
    m = re.search(r"try again in\s+(?:(\d+)\s*m)?\s*(?:([\d.]+)\s*s)?", err, re.IGNORECASE)
    if m:
        mins = float(m.group(1)) if m.group(1) else 0.0
        secs = float(m.group(2)) if m.group(2) else 0.0
        total = mins * 60 + secs
        if total > 0:
            return total
    # 退而求其次：'retry_after: X' 之類
    m2 = re.search(r"retry[_-]?after['\":\s]+([\d.]+)", err, re.IGNORECASE)
    if m2:
        return float(m2.group(1))
    return default


@dataclass
class PoolEndpoint:
    """池中的一個 endpoint。client/model 給 caller 拿去呼叫；其餘是 pool 的狀態。"""
    name: str
    client: Any = None
    model: str = ""
    tpm_budget: int = 6000
    cooldown_until: float = 0.0
    usage_window: deque = field(default_factory=deque)  # (ts, tokens) 滾動 60s


class CooldownAwarePool:
    TPM_HEADROOM = 0.75      # 用量 > budget*headroom 就跳，留 buffer 不撞線
    USAGE_WINDOW_S = 60.0

    def __init__(self, endpoints: list[PoolEndpoint], *,
                 clock: Callable[[], float] = time.time):
        self.endpoints = list(endpoints)   # 註冊順序 = 優先序
        self._clock = clock

    def current_tpm(self, ep: PoolEndpoint) -> int:
        """滾動 60s 視窗內的 token 用量（順便清掉過期項）。"""
        cutoff = self._clock() - self.USAGE_WINDOW_S
        while ep.usage_window and ep.usage_window[0][0] < cutoff:
            ep.usage_window.popleft()
        return sum(tok for _, tok in ep.usage_window)

    def next_available(self) -> Optional[PoolEndpoint]:
        """按優先序回第一個沒在冷卻、TPM 未近上限的 endpoint；全滿回 None。"""
        now = self._clock()
        for ep in self.endpoints:
            if now < ep.cooldown_until:
                continue
            if self.current_tpm(ep) > ep.tpm_budget * self.TPM_HEADROOM:
                continue
            return ep
        return None

    def mark_429(self, ep: PoolEndpoint, err_str: str = "", *,
                 retry_after: Optional[float] = None) -> None:
        """endpoint 被 429 → 設冷卻到 now + retry_after（從錯誤訊息解析或顯式給）。"""
        secs = retry_after if retry_after is not None else parse_retry_after(err_str)
        ep.cooldown_until = self._clock() + secs

    def record_usage(self, ep: PoolEndpoint, tokens: int) -> None:
        """呼叫成功後回報用量，進滾動 TPM 視窗。"""
        ep.usage_window.append((self._clock(), max(0, int(tokens))))
