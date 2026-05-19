"""DeclarativeIntentAgent — Amazon Alexa-style declarative intent base.

每個 agent 用 declarative IntentSchema 列舉自己會接的意圖（regex pattern + slots +
confidence），bid() 由 base class 自動實作：

  gate(ctx)               — 早退條件（low wake_intent / empty query / hallucination）
                            回 reason str → dense Bid(0.0, reason=gate_reason)
  declare_intents()       — list[IntentSchema]，order = priority（first match wins）
  post_match_filter()     — schema 命中後的精細邏輯（regex 無法表達的，如 blocklist）
                            回 False → 繼續找下個 schema
  make_handler()          — schema + slots → coroutine（接到 controller 的實際 action）

Bid 永遠 dense：未命中 schema 也回 Bid(confidence=0.0, reason="no_match")，讓未來
70B verifier 看到「我看了，不是我」的明確 negative signal（5/19 Q3 verifier_replay 發現
2-agent 規模 bid vector 太稀就是這個問題）。

設計來源：5/19 設計討論，已記憶 `project_intent_bus.md`。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from intent_bus import Bid, IntentContext


@dataclass(frozen=True)
class IntentSchema:
    """Declarative intent: name + regex patterns + slot spec.

    patterns: list of regex strings; first hit wins. Named groups in pattern
              become slots (passed to reason_template + handler).
    required_slots: slot names that, if missing/empty, get added to missing_slots
                    (Alexa CanFulfillIntent 模式).
    reason_template: format string using slots dict + extra keys {matched, name}.
    """
    name: str
    confidence: float
    patterns: list[str]
    required_slots: list[str] = field(default_factory=list)
    reason_template: str = "{name}"


class DeclarativeIntentAgent:
    """Base for Amazon-style declarative intent agents."""

    name: str = "<override>"

    # Modes this agent participates in. Override in subclass — e.g.:
    #   MusicAgent:       {"normal", "stream"}    （遊戲中不該誤觸發音樂指令）
    #   Busted99Agent:    {"game"}               （只在遊戲模式吃 raw 語音）
    # 預設 {"normal"}：保守，subclass 必須顯式宣告才能在其他模式出價。
    mode_compatible: set[str] = frozenset({"normal"})

    # ── Subclass hooks ────────────────────────────────────────────────────────

    def declare_intents(self) -> list[IntentSchema]:
        raise NotImplementedError("subclass must implement declare_intents()")

    def gate(self, ctx: IntentContext) -> str | None:
        """Subclass-overridable gate. Return reason string to short-circuit
        with dense 0.0 bid; None to proceed.

        NOTE: mode compatibility is checked separately by `bid()` before this
        runs, so subclass overrides do NOT need to call super().gate().
        """
        return None

    def post_match_filter(
        self, schema: IntentSchema, slots: dict[str, str], ctx: IntentContext
    ) -> bool:
        """Optional fine-grained filter after regex match.

        Return False to reject this schema match and continue to next schema.
        Default: accept all.
        """
        return True

    def make_handler(
        self, schema: IntentSchema, slots: dict[str, str], ctx: IntentContext
    ) -> Callable[[], Awaitable[None]]:
        """Build coroutine to run when this bid wins. Default = noop."""
        return self._noop

    # ── Auto-implemented bid() ────────────────────────────────────────────────

    def bid(self, ctx: IntentContext) -> Bid:
        # Pre-gate: mode compatibility (non-overridable; subclass can't bypass)
        if ctx.mode not in self.mode_compatible:
            return self._dense_zero(f"mode_mismatch:{ctx.mode}")

        # Subclass-overridable gate
        gate_reason = self.gate(ctx)
        if gate_reason is not None:
            return self._dense_zero(gate_reason)

        text = (ctx.query or "").strip()
        if not text:
            return self._dense_zero("empty_query")

        for schema in self.declare_intents():
            for pat in schema.patterns:
                m = re.search(pat, text, re.IGNORECASE)
                if not m:
                    continue
                slots = {k: (v or "") for k, v in m.groupdict().items()}
                if not self.post_match_filter(schema, slots, ctx):
                    continue
                missing = [s for s in schema.required_slots
                           if not slots.get(s, "").strip()]
                fmt_kwargs = {**slots, "matched": m.group(0), "name": schema.name}
                try:
                    reason = schema.reason_template.format(**fmt_kwargs)
                except (KeyError, IndexError):
                    reason = schema.name
                return Bid(
                    name=self.name,
                    confidence=schema.confidence,
                    handler=self.make_handler(schema, slots, ctx),
                    reason=reason,
                    missing_slots=missing,
                )

        return self._dense_zero("no_match")

    # ── Internals ─────────────────────────────────────────────────────────────

    def _dense_zero(self, reason: str) -> Bid:
        return Bid(name=self.name, confidence=0.0, handler=self._noop, reason=reason)

    async def _noop(self) -> None:
        pass
