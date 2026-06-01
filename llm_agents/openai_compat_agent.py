"""OpenAICompatAgent — 通用 OpenAI-相容 provider agent。

GroqAgent / CerebrasAgent 結構幾乎一樣，差別只在 provider name / endpoint /
priority / latency 估計。本 class 把這些參數化，一個 class 接多個 OpenAI-compat
provider（SambaNova / Together / OpenRouter），不必每家寫一個 class。

6/2：原本 bus 只有 Groq + Cerebras 兩個 agent，雙雙 429 時無人可接 → 回 ''
→ 喚醒回應拿不到 LLM。把 .env 已有 key 但沒 agent 的 3 個 provider 接進來分攤。

confidence 比 Groq/Cerebras（base 0.65）略低 → 當備援，平時讓快又熟的兩家先接。
"""
from __future__ import annotations

import logging

from llm_agents.base import LLMAgent, LLMBid, LLMContext
from llm_agents.quota_service import QuotaService

logger = logging.getLogger("MarvinBot.LLMBus.OpenAICompat")


class OpenAICompatAgent(LLMAgent):
    """Generic OpenAI-compatible provider agent。

    endpoint 命名跟 build_tier_pools 一致：f"{provider_name}-quick" / "-analyze"。
    """

    QUICK_LATENCY_MS = 800
    ANALYZE_LATENCY_MS = 2500
    BASE_CONFIDENCE = 0.50  # < Groq/Cerebras 0.65，當備援
    TPM_PRESSURE_PENALTY = 0.30

    def __init__(self, quota: QuotaService, *, provider_name: str, priority: int = 20):
        self.quota = quota
        self.name = provider_name
        self.providers = frozenset({provider_name})
        self.purpose_compatible = frozenset()  # 全 purpose
        self.priority = priority
        self._quick = f"{provider_name}-quick"
        self._analyze = f"{provider_name}-analyze"

    def _pick_endpoint_name(self, ctx: LLMContext) -> str:
        return self._analyze if ctx.min_quality == "high" else self._quick

    def bid(self, ctx: LLMContext) -> LLMBid:
        endpoint_name = self._pick_endpoint_name(ctx)
        state = self.quota.state(endpoint_name)

        if state is None:
            return LLMBid(0.0, self.name, "?", 0, 0,
                          f"endpoint_not_registered:{endpoint_name}")
        if state.cooldown_remaining_s > 0:
            return LLMBid(0.0, self.name, state.model, 0, 0,
                          f"cooldown:{state.cooldown_remaining_s:.0f}s")
        if not state.available:
            return LLMBid(0.0, self.name, state.model, 0, 0,
                          f"tpm_high:{state.tpm_used}/{state.tpm_budget}")

        # ① 壓力取 TPM 與 daily 較大者
        confidence = max(0.30, self.BASE_CONFIDENCE - max(state.tpm_ratio, state.daily_ratio) * self.TPM_PRESSURE_PENALTY)
        latency = (self.ANALYZE_LATENCY_MS if endpoint_name == self._analyze
                   else self.QUICK_LATENCY_MS)
        est_tokens = max(20, len(ctx.prompt) // 4)
        return LLMBid(confidence, self.name, state.model, latency, est_tokens, "happy")

    async def handle(self, ctx: LLMContext) -> str:
        endpoint_name = self._pick_endpoint_name(ctx)
        ep = self.quota.endpoint(endpoint_name)
        if ep is None:
            raise RuntimeError(f"[{self.name}] endpoint {endpoint_name} not registered")

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
