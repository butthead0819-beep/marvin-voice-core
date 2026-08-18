"""
GapNotifyMixin — VoiceController 的 intent gap owner DM 子系統。

從 voice_controller.py 抽出（減肥，line budget 棘輪只准降，見
test_voice_controller_size_budget.py）。self 仍是 VoiceController 實例，
self.bot / logger 沿用原本 self 存取，行為零改動。

8/18：每筆 gap classifier 記錄（agent_gaps.jsonl 那筆）即時 DM owner，取代人工
翻 log 的一次性流程。DM 純觀測用途，跟 main_discord.py 的 ErrorDispatcher._dm_owner
同一套「失敗吞掉、絕不拖慢 pipeline」原則——fire-and-forget create_task。
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class GapNotifyMixin:
    def _dm_owner_intent_gap(self, gap_rec) -> None:
        from cogs.voice_controller import _NEMOCLAW_OWNER_ID
        if not _NEMOCLAW_OWNER_ID:
            return

        async def _send() -> None:
            try:
                owner = self.bot.get_user(_NEMOCLAW_OWNER_ID) or await self.bot.fetch_user(_NEMOCLAW_OWNER_ID)
                if owner is None:
                    return
                text = (
                    f"🪦 [開放意圖] {gap_rec.speaker}: {gap_rec.cleaned_query}\n"
                    f"type={gap_rec.intent_type} nearest={gap_rec.nearest_agent} "
                    f"acked={gap_rec.acknowledged}"
                )
                await owner.send(text[:1990])
            except Exception:
                logger.debug("[IntentGap] DM owner 失敗（忽略）", exc_info=True)

        try:
            asyncio.create_task(_send())
        except RuntimeError:
            pass  # 無 running loop（理論上不會發生在此 async 路徑）
