"""BustedAgent — game-mode intent agent for the buzz-and-answer game (cogs/game_cog.py BustedCog).

Bid 模型：BustedCog 的「搶答 buzz 視窗」是 active 信號 — 當某玩家按 buzz 鍵後，
window 開啟，只有 buzz_holder 能回答。Agent bid 0.95 當：
  - mode == "game"
  - cog._session.buzz_holder_id 不為 None（buzz window 開啟）
  - speaker 是 buzz_holder（即 cog.should_suppress_for_game 回 False）

Coupling note: 同 Busted99Agent，introspect `cog._session.buzz_holder_id` private state。
"""
from __future__ import annotations

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_bus import Bid, IntentContext


class BustedAgent(DeclarativeIntentAgent):
    name = "busted"
    mode_compatible = frozenset({"game"})

    def __init__(self, bot):
        self.bot = bot

    def _get_cog(self):
        return self.bot.cogs.get("BustedCog") if self.bot else None

    def _has_open_buzz(self, cog) -> bool:
        session = getattr(cog, "_session", None)
        if session is None:
            return False
        return getattr(session, "buzz_holder_id", None) is not None

    def bid(self, ctx: IntentContext) -> Bid:
        if ctx.mode not in self.mode_compatible:
            return self._dense_zero(f"mode_mismatch:{ctx.mode}")

        text = (ctx.raw_text or ctx.query or "").strip()
        if not text:
            return self._dense_zero("empty_text")

        cog = self._get_cog()
        if cog is None:
            return self._dense_zero("cog_not_loaded")

        if not self._has_open_buzz(cog):
            return self._dense_zero("no_buzz_window")

        if cog.should_suppress_for_game(ctx.speaker):
            return self._dense_zero("not_buzz_holder")

        async def _handler():
            await cog.receive_voice_answer_by_speaker(ctx.speaker, text)

        return Bid(
            name=self.name,
            confidence=0.95,
            handler=_handler,
            reason="busted:buzz_open",
        )

    def declare_intents(self) -> list[IntentSchema]:
        return []  # not used; bid() overridden
