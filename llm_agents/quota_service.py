"""QuotaService — thin shim wraps llm_pool.CooldownAwarePool for sync state queries.

LLMAgent.bid() 必須 sync ≤5ms，不能跑 I/O。但 bid 需要知道 endpoint 是不是冷卻中、
TPM 用了多少、cooldown 還剩多少。llm_pool 既有 CooldownAwarePool 都已維護這些狀態，
QuotaService 只負責把它們以 EndpointState dataclass 形式 expose 給 bid()，並把
mutation (record_usage / mark_429) forward 回去。

不重做 TPM tracking、不重做 cooldown 邏輯 — 完全 reuse llm_pool 既有實作（Phase 3
才考慮是否拆解 llm_pool 內部結構）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from llm_pool import CooldownAwarePool, PoolEndpoint


@dataclass(frozen=True)
class EndpointState:
    """sync snapshot of a pool endpoint state — agent.bid() 用的純 read 視圖。"""
    name: str
    model: str
    available: bool             # not cooled AND TPM ≤ headroom
    tpm_used: int
    tpm_budget: int
    tpm_ratio: float            # tpm_used / max(tpm_budget, 1)
    cooldown_remaining_s: float # 0.0 if not cooled


class QuotaService:
    """Sync state lookup + mutation forwarder for one or more CooldownAwarePool."""

    def __init__(self, pools: list[CooldownAwarePool]):
        # endpoint name → (pool, endpoint) 索引
        self._index: dict[str, tuple[CooldownAwarePool, PoolEndpoint]] = {}
        for pool in pools:
            for ep in pool.endpoints:
                self._index[ep.name] = (pool, ep)

    def state(self, name: str) -> Optional[EndpointState]:
        record = self._index.get(name)
        if record is None:
            return None
        pool, ep = record
        now = pool._clock()
        tpm = pool.current_tpm(ep)
        cooldown_remaining = max(0.0, ep.cooldown_until - now)
        is_cooled = cooldown_remaining == 0.0
        within_headroom = tpm <= ep.tpm_budget * pool.TPM_HEADROOM
        ratio = tpm / max(ep.tpm_budget, 1)
        return EndpointState(
            name=ep.name,
            model=ep.model,
            available=is_cooled and within_headroom,
            tpm_used=tpm,
            tpm_budget=ep.tpm_budget,
            tpm_ratio=ratio,
            cooldown_remaining_s=cooldown_remaining,
        )

    def record_usage(self, name: str, tokens: int) -> None:
        """Forward to underlying pool. Unknown name = silent no-op (caller 拼錯名最多漏記 metric)."""
        record = self._index.get(name)
        if record is not None:
            pool, ep = record
            pool.record_usage(ep, tokens)

    def mark_429(self, name: str, err_str: str = "", *, retry_after: Optional[float] = None) -> None:
        """Forward to underlying pool's cooldown logic. Unknown name = no-op."""
        record = self._index.get(name)
        if record is not None:
            pool, ep = record
            pool.mark_429(ep, err_str, retry_after=retry_after)
