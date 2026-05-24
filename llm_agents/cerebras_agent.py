"""CerebrasAgent — Plan C7. 跟 GroqAgent 結構幾乎一樣，差別:
- endpoints: cerebras-quick (llama3.1-8b) / cerebras-analyze (qwen-3-235b)
- tpm_budget 大 10×（60K vs Groq 6K）
- latency 預估更低（Cerebras 「超高速免費備援」）
- priority 介於 Groq 跟 Gemini 之間
"""
from __future__ import annotations

import logging
from llm_agents.base import LLMAgent, LLMBid, LLMContext
from llm_agents.quota_service import QuotaService

logger = logging.getLogger("MarvinBot.LLMBus.Cerebras")


class CerebrasAgent(LLMAgent):
    name = "cerebras"
    providers = frozenset({"cerebras"})
    purpose_compatible: frozenset[str] = frozenset()
    priority = 15  # 低數字 bid 先；介於 Groq (10) 跟 Gemini (>20) 之間

    QUICK_ENDPOINT = "cerebras-quick"
    ANALYZE_ENDPOINT = "cerebras-analyze"

    # Cerebras 號稱比 Groq 快
    QUICK_LATENCY_MS = 200
    ANALYZE_LATENCY_MS = 1000

    BASE_CONFIDENCE = 0.65
    TPM_PRESSURE_PENALTY = 0.30

    def __init__(self, quota: QuotaService):
        self.quota = quota

    def _pick_endpoint_name(self, ctx: LLMContext) -> str:
        if ctx.min_quality == "high":
            return self.ANALYZE_ENDPOINT
        return self.QUICK_ENDPOINT

    def bid(self, ctx: LLMContext) -> LLMBid:
        endpoint_name = self._pick_endpoint_name(ctx)
        state = self.quota.state(endpoint_name)

        if state is None:
            return LLMBid(0.0, "cerebras", "?", 0, 0, f"endpoint_not_registered:{endpoint_name}")

        if state.cooldown_remaining_s > 0:
            return LLMBid(0.0, "cerebras", state.model, 0, 0,
                          f"cooldown:{state.cooldown_remaining_s:.0f}s")

        if not state.available:
            return LLMBid(0.0, "cerebras", state.model, 0, 0,
                          f"tpm_high:{state.tpm_used}/{state.tpm_budget}")

        confidence = self.BASE_CONFIDENCE - state.tpm_ratio * self.TPM_PRESSURE_PENALTY
        confidence = max(0.30, confidence)
        latency = (self.ANALYZE_LATENCY_MS if endpoint_name == self.ANALYZE_ENDPOINT
                   else self.QUICK_LATENCY_MS)
        est_tokens = max(20, len(ctx.prompt) // 4)

        return LLMBid(
            confidence=confidence,
            provider="cerebras",
            model=state.model,
            estimated_latency_ms=latency,
            estimated_cost_units=est_tokens,
            reason="happy",
        )

    async def handle(self, ctx: LLMContext) -> str:
        endpoint_name = self._pick_endpoint_name(ctx)
        ep = self.quota.endpoint(endpoint_name)
        if ep is None:
            raise RuntimeError(f"[CerebrasAgent] endpoint {endpoint_name} not registered")

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
            if any(h in err_str.lower() for h in ("429", "rate limit", "rate_limit", "quota")):
                self.quota.mark_429(endpoint_name, err_str)
            else:
                self.quota.mark_429(endpoint_name, retry_after=5.0)
            raise

        content = resp.choices[0].message.content
        usage = getattr(resp, "usage", None)
        tokens = usage.total_tokens if usage else 0
        self.quota.record_usage(endpoint_name, tokens)
        return content or ""
