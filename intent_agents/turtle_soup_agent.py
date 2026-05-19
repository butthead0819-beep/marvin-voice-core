"""TurtleSoupAgent — game-mode intent agent for 海龜湯（cogs/turtle_soup_cog.py TurtleSoupCog）。

最乾淨的 cog——有 public `is_active()` 不需 introspect private state。
但 `receive_voice_answer_by_speaker` 只在 ASKING 階段真正消化，所以 agent 進一步檢查
state 字串以避免在 PRESENTING / JOINING 階段誤搶 bid。
"""
from __future__ import annotations

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_bus import Bid, IntentContext


class TurtleSoupAgent(DeclarativeIntentAgent):
    name = "turtle_soup"
    mode_compatible = frozenset({"game"})

    def __init__(self, bot):
        self.bot = bot

    def _get_cog(self):
        return self.bot.cogs.get("TurtleSoupCog") if self.bot else None

    def _is_asking(self, cog) -> bool:
        """ASKING 才會真正消化語音，其他 active state (JOINING/PRESENTING) bid 0.0。"""
        session = getattr(cog, "_session", None)
        if session is None:
            return False
        state = getattr(session, "state", None)
        return getattr(state, "name", "") == "ASKING"

    def bid(self, ctx: IntentContext) -> Bid:
        if ctx.mode not in self.mode_compatible:
            return self._dense_zero(f"mode_mismatch:{ctx.mode}")

        text = (ctx.raw_text or ctx.query or "").strip()
        if not text:
            return self._dense_zero("empty_text")

        cog = self._get_cog()
        if cog is None:
            return self._dense_zero("cog_not_loaded")

        is_active = getattr(cog, "is_active", lambda: False)()
        if not is_active:
            return self._dense_zero("not_active")

        if not self._is_asking(cog):
            return self._dense_zero("not_in_asking_state")

        async def _handler():
            await cog.receive_voice_answer_by_speaker(ctx.speaker, text)

        return Bid(
            name=self.name,
            confidence=0.95,
            handler=_handler,
            reason="turtle_soup:asking",
        )

    def declare_intents(self) -> list[IntentSchema]:
        return []
