"""LLM dispatch bus 核心 contract.

Bid pattern 沿用 intent_agents/base.py::DeclarativeIntentAgent（5/19 已驗證）：
- sync bid() ≤5ms 契約
- dense 0.0 + reason taxonomy
- max confidence wins

差別：
- LLM dispatch 不需要 IntentSchema (regex pattern)
- purpose_compatible 取代 mode_compatible（按 LLM 任務分流）
- stickiness 機制（F4：同 speaker 5 min 偏好同 provider，避免人格漂移）
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger("MarvinBot.LLMBus")


# ---------------------------------------------------------------------------
# Purpose 白名單 (F5: typo regression 防護)
# ---------------------------------------------------------------------------

# Phase 1 baseline — 隨 Phase 1/2 caller grep 後逐步擴充，Phase 3 (C14) 收斂成 enum.
KNOWN_PURPOSES: frozenset[str] = frozenset({
    "marvin_chat",      # Marvin LLM chat (主要 marvin response)
    "cleaner",          # STT cleaner LLM call
    "wake_classify",    # wake detection LLM hint
    "song_meta",        # 歌曲 metadata 解析
    "topic_gen",        # 話題生成
    "dj_quip",          # DJ 賭一把 / interjection
    "summarizer",       # conversation summary
    "intent_classify",  # intent / query 分類
})


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LLMContext:
    """One LLM dispatch request.

    purpose 是 Phase 1 free string，KNOWN_PURPOSES 是 typo 警示白名單（不擋 dispatch）。
    speaker 是 stickiness key — None 表示系統呼叫（cron / background）跳過 stickiness。
    system_prompt / json_mode / temperature / max_tokens 直接 forward 給 agent.handle
    打 OpenAI-相容 API；None 走 agent 預設值。
    """
    prompt: str
    purpose: str
    speaker: str | None = None
    latency_budget_ms: int | None = None
    min_quality: Literal["fast", "balanced", "high"] = "balanced"
    max_cost_units: int | None = None
    system_prompt: str | None = None
    json_mode: bool = False
    temperature: float | None = None
    max_tokens: int | None = None


@dataclass(frozen=True)
class LLMBid:
    """Agent 給某 LLMContext 的出價。

    confidence 0.0 = 不接（dense 0.0 with reason）。reason 在 dense 0.0 必須 distinct（
    例：tpm_exhausted / cooldown / purpose_mismatch / quality_too_low / latency_too_tight），
    避免全寫 "no_match" 失去診斷價值。
    """
    confidence: float
    provider: str
    model: str
    estimated_latency_ms: int
    estimated_cost_units: int
    reason: str


class NoLLMAvailable(Exception):
    """全 agent dense 0.0 / 低於 MIN_CONFIDENCE — caller 兜底處理（legacy fallback 或 raise）。"""


@dataclass(frozen=True)
class DispatchMetadata:
    """Observability — bus.last_dispatch 記錄上次 dispatch 的勝者，給 metrics writer 用。"""
    winner_provider: str
    winner_model: str
    winner_agent: str
    winner_confidence: float
    bid_summary: tuple  # tuple of (agent_name, confidence, reason)


# ---------------------------------------------------------------------------
# Agent base
# ---------------------------------------------------------------------------

class LLMAgent:
    """LLM provider agent base class.

    子類必填 attribute:
    - `name`：log / observability key
    - `providers`：本 agent 認領的 provider 字串集合
    - `purpose_compatible`：本 agent 擅長的 purpose 集合；空 set 表「全擅長」
    - `priority`：lower bid first（給 F3 short-circuit 用），預設 50

    bid() 必須 sync ≤5ms；handle() async（真實 LLM 呼叫）。
    """

    name: str = "base"
    providers: frozenset[str] = frozenset()
    purpose_compatible: frozenset[str] = frozenset()
    priority: int = 50

    def bid(self, ctx: LLMContext) -> LLMBid:
        """Return LLMBid synchronously. NEVER call I/O or LLM here."""
        raise NotImplementedError

    async def handle(self, ctx: LLMContext) -> str:
        """Actual LLM call. Async, can take seconds."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Bus
# ---------------------------------------------------------------------------

class LLMBus:
    """Dispatch a LLMContext to the best-bidding agent.

    Selection logic:
      1. F3 short-circuit: bid 前 _SHORT_CIRCUIT_AFTER 個 agent (priority asc sorted)
      2. F4 stickiness: 同 speaker 上次贏家 +_STICKINESS_BONUS, TTL 內有效
      3. Filter confidence ≥ _MIN_CONFIDENCE
      4. Max confidence wins, tie-break by estimated_latency_ms asc
      5. 全 0.0 / 低於 threshold → raise NoLLMAvailable
    """

    _MIN_CONFIDENCE: float = 0.30
    _SHORT_CIRCUIT_AFTER: int = 3
    _PROVIDER_STICKINESS_TTL: float = 300.0  # 秒
    _STICKINESS_BONUS: float = 0.10
    # ④ degraded 告警：可用 provider 數 ≤ 此值 → 告警（debounce 避免洗版）
    _DEGRADED_THRESHOLD: int = 1
    _DEGRADED_DEBOUNCE_S: float = 300.0

    def __init__(self, agents: list[LLMAgent], *, on_degraded=None):
        # priority asc — 數字小的優先 bid
        self._agents: list[LLMAgent] = sorted(agents, key=lambda a: a.priority)
        # speaker -> (provider, monotonic_ts)
        self._sticky: dict[str, tuple[str, float]] = {}
        # 上次 dispatch 勝者 metadata，給 metrics writer 用
        self.last_dispatch: DispatchMetadata | None = None
        # ④ 掉線告警 callback：callable(viable_count:int, bid_summary:str) | None
        self.on_degraded = on_degraded
        self._last_degraded_ts: float = 0.0

    async def dispatch(self, ctx: LLMContext) -> str:
        # F5: purpose typo warning
        if ctx.purpose not in KNOWN_PURPOSES:
            logger.warning("[LLMBus] unknown purpose %r — typo? known: %s",
                           ctx.purpose, sorted(KNOWN_PURPOSES))

        # F3: short-circuit — 只 bid 前 K 個（priority sorted）
        candidates = self._agents[:self._SHORT_CIRCUIT_AFTER]
        bids: list[tuple[LLMAgent, LLMBid, float]] = []  # (agent, bid, adjusted_confidence)

        # F4: stickiness lookup
        sticky_provider = self._get_sticky_provider(ctx.speaker)

        for agent in candidates:
            try:
                bid = agent.bid(ctx)
            except Exception as e:
                # 一個 agent 炸不影響其他（同 IntentBus 規範）
                logger.exception("[LLMBus] agent %s bid raised: %s", agent.name, e)
                continue

            adjusted = bid.confidence
            if sticky_provider and bid.provider == sticky_provider:
                adjusted += self._STICKINESS_BONUS
            bids.append((agent, bid, adjusted))

        # Filter
        viable = [(a, b, c) for (a, b, c) in bids if c >= self._MIN_CONFIDENCE]

        # ④ degraded 偵測：可用 provider 數過低 → debounced 告警（不阻斷 dispatch）
        distinct_viable = len({b.provider for (_, b, _) in viable})
        if distinct_viable <= self._DEGRADED_THRESHOLD:
            self._maybe_alert_degraded(distinct_viable, bids)

        if not viable:
            reasons = [(a.name, b.reason) for (a, b, _) in bids]
            raise NoLLMAvailable(
                f"all {len(bids)} agents below threshold ({self._MIN_CONFIDENCE}); "
                f"reasons={reasons}"
            )

        # Sort: confidence desc, then latency asc
        viable.sort(key=lambda t: (-t[2], t[1].estimated_latency_ms))
        winner_agent, winner_bid, winner_conf = viable[0]

        # 記 metadata 給 metrics 用
        self.last_dispatch = DispatchMetadata(
            winner_provider=winner_bid.provider,
            winner_model=winner_bid.model,
            winner_agent=winner_agent.name,
            winner_confidence=winner_conf,
            bid_summary=tuple((a.name, b.confidence, b.reason) for (a, b, _) in bids),
        )

        try:
            result = await winner_agent.handle(ctx)
        except Exception:
            # F4 caveat: 如果 sticky provider handle() 拋例外，清掉 stickiness 避免下次再黏
            if ctx.speaker and sticky_provider == winner_bid.provider:
                self._sticky.pop(ctx.speaker, None)
            raise

        # F4: 記錄 stickiness（只在有 speaker 時）
        if ctx.speaker:
            self._sticky[ctx.speaker] = (winner_bid.provider, time.monotonic())

        return result

    def _maybe_alert_degraded(self, viable_count: int, bids: list) -> None:
        """④ 可用 provider 過低 → loud log + callback（debounce 避免每次 dispatch 洗版）。"""
        now = time.monotonic()
        if now - self._last_degraded_ts < self._DEGRADED_DEBOUNCE_S:
            return
        self._last_degraded_ts = now
        summary = ", ".join(f"{a.name}={b.reason}" for (a, b, _) in bids) or "no_bids"
        # ERROR 級 → 掛 root logger 的 ErrorDispatcher 自動接走 → openclaw triage → DM owner
        # （MarvinBot.LLMBus 不在 ErrorDispatcher 黑名單）。300s debounce 防 DM 洗版。
        logger.error(
            f"🚨 [LLMBus DEGRADED] 可用 LLM provider 僅剩 {viable_count} 個（≤{self._DEGRADED_THRESHOLD}）！"
            f"bids: {summary}"
        )
        if self.on_degraded is not None:
            try:
                self.on_degraded(viable_count, summary)
            except Exception as e:
                logger.warning(f"[LLMBus] on_degraded callback raised: {e}")

    def _get_sticky_provider(self, speaker: str | None) -> str | None:
        """回 speaker 上次 dispatch 的 provider，TTL 內有效；過期 / 無前史 → None。"""
        if speaker is None:
            return None
        record = self._sticky.get(speaker)
        if record is None:
            return None
        provider, ts = record
        if time.monotonic() - ts > self._PROVIDER_STICKINESS_TTL:
            self._sticky.pop(speaker, None)
            return None
        return provider
