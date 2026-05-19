"""Busted99Agent — game-mode intent agent for the 21-point number-guessing game.

Vertical slice: 把 voice_controller L2400-2410 的 game cog dispatch chain 中的
Busted99Cog 部分搬進 IntentBus 框架。

Architecture notes:
  - mode_compatible = {"game"}：只在遊戲模式出價（normal/stream 時自動 dense-0.0）
  - bid 不需 wake gate：遊戲模式吃 raw 語音，與 MusicAgent 不同
  - bid confidence = 0.95 when (session active in GUESSING + speaker is current guesser)
  - 其他狀態 → dense Bid(0.0, reason=...) 把 negative space 表達清楚
  - handler 直接 await cog.receive_voice_answer_by_speaker(speaker, raw_text)

Coupling note: 目前 introspect `cog._session` private state 因為 cog 沒 public
`is_active()` helper。Vertical slice 階段可接受；後續多個 game agent 上線後抽
共用 `GameCogProtocol` 把 active check 標準化。
"""
from __future__ import annotations

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_bus import Bid, IntentContext


class Busted99Agent(DeclarativeIntentAgent):
    """Bids 0.95 when Busted99 session is in GUESSING state and speaker is the current guesser."""

    name = "busted99"
    mode_compatible = frozenset({"game"})

    def __init__(self, bot):
        # bot 而非 cog — 因為 cog 可能 hot-reload，每次 bid 查 latest
        self.bot = bot

    def _get_cog(self):
        return self.bot.cogs.get("Busted99Cog") if self.bot else None

    def _is_guessing(self, cog) -> bool:
        """Check session state without importing Busted99State enum (avoid hard dep)."""
        session = getattr(cog, "_session", None)
        if session is None:
            return False
        state = getattr(session, "state", None)
        # state.name == "GUESSING"（enum value）；用字串比避免 import 循環
        return getattr(state, "name", "") == "GUESSING"

    # ── Custom bid (skip declare_intents — game agent 不走 regex 路徑) ──────

    def bid(self, ctx: IntentContext) -> Bid:
        # Pre-gate: mode (base class would also do this, but we don't call super().bid()
        # since game agent doesn't use IntentSchema pattern matching)
        if ctx.mode not in self.mode_compatible:
            return self._dense_zero(f"mode_mismatch:{ctx.mode}")

        text = (ctx.raw_text or ctx.query or "").strip()
        if not text:
            return self._dense_zero("empty_text")

        cog = self._get_cog()
        if cog is None:
            return self._dense_zero("cog_not_loaded")

        if not self._is_guessing(cog):
            return self._dense_zero("not_in_guessing_state")

        # 用 cog 自己的 suppress 判斷（封裝了 current_guesser 比對邏輯）
        if cog.should_suppress_for_game(ctx.speaker):
            return self._dense_zero("not_current_guesser")

        # 通過所有檢查 → bid 0.95
        async def _handler():
            await cog.receive_voice_answer_by_speaker(ctx.speaker, text)

        return Bid(
            name=self.name,
            confidence=0.95,
            handler=_handler,
            reason="busted99:guessing",
        )

    def declare_intents(self) -> list[IntentSchema]:
        # Game agents 不用 declarative schema — bid 邏輯純粹靠遊戲狀態
        # 仍要實作（abstract method），但 bid() 已 override 不會走到這
        return []
