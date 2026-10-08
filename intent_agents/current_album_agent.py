"""CurrentAlbumAgent —「播放這張專輯」→ 正在播的這首歌的專輯其他歌排進佇列（10/8）。

跟 /tour（整張巡禮、每首先導聆）不同：這裡是把目前這首歌的專輯其他歌挑幾首接著排進
一般佇列，不打斷別人點歌、不換 mode。handler 本體在 MusicCommandsMixin.queue_current_album
（守 voice_controller.py size budget 棘輪）。
"""
from __future__ import annotations

import logging

from intent_agents.base import DeclarativeIntentAgent, IntentSchema, is_audio_rescue
from intent_bus import IntentContext

logger = logging.getLogger(__name__)


class CurrentAlbumAgent(DeclarativeIntentAgent):
    name = "current_album"
    mode_compatible = frozenset({"normal", "stream"})

    def __init__(self, controller):
        self.ctrl = controller
        self._cache: list[IntentSchema] | None = None

    def declare_intents(self) -> list[IntentSchema]:
        if self._cache is None:
            self._cache = [
                IntentSchema(
                    "play_current_album", 0.97,
                    patterns=[r"(?:播放?|放|聽|來點|來)(?:一下|一點)?\s*這一?張(?:專輯)?"],
                    required_slots=[],
                    reason_template="current_album",
                    manifest_description=(
                        "使用者要聽『現在正在播的這首歌』所屬那張專輯的其他歌（例：播放這張專輯、"
                        "這張專輯其他歌也放一下、整張來聽聽、這專輯還有什麼都排進來）。不需要講出"
                        "專輯名；使用者講出具體專輯名時改用 find_album。不是問專輯資訊。"
                    ),
                ),
            ]
        return self._cache

    def gate(self, ctx: IntentContext) -> str | None:
        if is_audio_rescue(ctx):
            return None
        if getattr(ctx.rescue, "low_confidence_wake", False):
            return "low_confidence_wake"
        return None

    def make_handler(self, schema, slots, ctx: IntentContext):
        async def _answer():
            mc = self.ctrl.bot.cogs.get("MusicCog")
            if mc is None:
                logger.warning("📀 [CurrentAlbum] MusicCog 未載入，放棄")
                return
            await mc.queue_current_album(ctx.speaker)
        return _answer
