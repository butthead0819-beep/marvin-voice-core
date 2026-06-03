"""GroqAgent — Plan C3 第一個 LLMAgent concrete impl.

設計選擇：
- per-provider 顆粒度（Q2=A）：8B / 70B model 在 agent 內依 min_quality 挑
- 不限 purpose（purpose_compatible=空 = 全擅長），但 confidence 隨 TPM 壓力衰減
- handle() 透過 QuotaService.endpoint(name) 拿 PoolEndpoint client，sync record_usage / mark_429

Bid confidence 公式（Phase 1 簡化版）：
- 不可用（cooldown / tpm_high / endpoint missing）→ dense 0.0 with distinct reason
- 可用 → base 0.65 - tpm_ratio × 0.30 → range [0.35, 0.65]
  - tpm 0% used → 0.65（happy path）
  - tpm 50% used → 0.50
  - tpm 75% (headroom 邊界) → 約 0.43，仍過 MIN_CONFIDENCE 0.30 門檻；超過 75% 就被 quota 擋掉
"""
from __future__ import annotations

import logging
from llm_agents.base import BACKGROUND_PURPOSES, LLMAgent, LLMBid, LLMContext
from llm_agents.quota_service import QuotaService

logger = logging.getLogger("MarvinBot.LLMBus.Groq")


class GroqAgent(LLMAgent):
    name = "groq"
    providers = frozenset({"groq"})
    purpose_compatible: frozenset[str] = frozenset()  # 全 purpose
    priority = 10  # 低數字 = bid 順序在前；Groq 8B 快又便宜

    QUICK_ENDPOINT = "groq-quick"        # 8B
    ANALYZE_ENDPOINT = "groq-analyze"    # 70B

    # 粗略 latency 估計（給 LLMBid.estimated_latency_ms / bus tiebreak 用）
    QUICK_LATENCY_MS = 400
    ANALYZE_LATENCY_MS = 1500

    BASE_CONFIDENCE = 0.65
    TPM_PRESSURE_PENALTY = 0.30  # 線性衰減
    # 背景/離線 purpose 在 Groq（最稀缺）上額外降權 → 讓位給 Cerebras，省 Groq 配額給 reactive。
    # 仍受 0.30 floor 保護：Groq 是唯一可用時背景照樣能跑（軟性偏好，非硬排除）。
    BACKGROUND_PENALTY = 0.20

    def __init__(self, quota: QuotaService):
        self.quota = quota

    def _pick_endpoint_name(self, ctx: LLMContext) -> str:
        if ctx.min_quality == "high":
            return self.ANALYZE_ENDPOINT
        # "fast" 跟 "balanced" 都走 quick；balanced 不偷偷 fallthrough 到 70B 預算爆掉
        return self.QUICK_ENDPOINT

    def bid(self, ctx: LLMContext) -> LLMBid:
        endpoint_name = self._pick_endpoint_name(ctx)
        state = self.quota.state(endpoint_name)

        if state is None:
            return LLMBid(
                confidence=0.0, provider="groq", model="?",
                estimated_latency_ms=0, estimated_cost_units=0,
                reason=f"endpoint_not_registered:{endpoint_name}",
            )

        if state.cooldown_remaining_s > 0:
            return LLMBid(
                confidence=0.0, provider="groq", model=state.model,
                estimated_latency_ms=0, estimated_cost_units=0,
                reason=f"cooldown:{state.cooldown_remaining_s:.0f}s",
            )

        if not state.available:
            return LLMBid(
                confidence=0.0, provider="groq", model=state.model,
                estimated_latency_ms=0, estimated_cost_units=0,
                reason=f"tpm_high:{state.tpm_used}/{state.tpm_budget}",
            )

        # Happy path — ① 壓力取 per-minute TPM 與當日 daily 較大者（daily 快爆時主動讓位）
        pressure = max(state.tpm_ratio, state.daily_ratio)
        confidence = self.BASE_CONFIDENCE - pressure * self.TPM_PRESSURE_PENALTY
        # ② 背景/離線 purpose 額外降權，把 Groq 留給 reactive（floor 後仍 ≥0.30 可用）
        if ctx.purpose in BACKGROUND_PURPOSES:
            confidence -= self.BACKGROUND_PENALTY
        confidence = max(0.30, confidence)  # floor 避免低 confidence 全部塞 dense 0.0 邊界
        latency = (self.ANALYZE_LATENCY_MS if endpoint_name == self.ANALYZE_ENDPOINT
                   else self.QUICK_LATENCY_MS)
        est_tokens = max(20, len(ctx.prompt) // 4)  # 粗估 token

        return LLMBid(
            confidence=confidence,
            provider="groq",
            model=state.model,
            estimated_latency_ms=latency,
            estimated_cost_units=est_tokens,
            reason="happy",
        )

    async def handle(self, ctx: LLMContext) -> str:
        endpoint_name = self._pick_endpoint_name(ctx)
        ep = self.quota.endpoint(endpoint_name)
        if ep is None:
            raise RuntimeError(f"[GroqAgent] endpoint {endpoint_name} not registered")

        messages = []
        if ctx.system_prompt:
            messages.append({"role": "system", "content": ctx.system_prompt})
        messages.append({"role": "user", "content": ctx.prompt})

        kwargs = dict(
            model=ep.model,
            messages=messages,
            temperature=ctx.temperature if ctx.temperature is not None else 0.7,
            max_tokens=ctx.max_tokens if ctx.max_tokens is not None else 1024,
            stream=False,
        )
        if ctx.json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            resp = await ep.client.chat.completions.create(**kwargs)
        except Exception as e:
            err_str = str(e)
            # 簡單 rate-limit 偵測（llm_pool.is_rate_limit 同邏輯，避免循環 import）
            if any(h in err_str.lower() for h in ("429", "rate limit", "rate_limit", "quota")):
                self.quota.mark_429(endpoint_name, err_str)
            else:
                # 非 429 暫時性錯（5xx / timeout）也短冷卻避免立刻重撞
                self.quota.mark_429(endpoint_name, retry_after=5.0)
            raise

        content = resp.choices[0].message.content
        usage = getattr(resp, "usage", None)
        tokens = usage.total_tokens if usage else 0
        self.quota.record_usage(endpoint_name, tokens)
        return content or ""
