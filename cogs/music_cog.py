"""MusicCog — 音樂子系統（從 VoiceController 抽離中）。

Phase 1 (骨架)：持有跨越 VoiceController 的跨切狀態（stream_mode、radio_mode）。
音樂邏輯仍在 VoiceController，逐步遷移中（Phase 2–6）。

遷移進度：
  Phase 1 ✅  骨架 + proxy properties
  Phase 2 ⬜  stream subsystem (_stream_loop, stream state)
  Phase 3 ⬜  radio subsystem (_radio_loop, radio state)
  Phase 4 ⬜  _auto_recommend + song metadata + DJ
  Phase 5 ⬜  slash commands (marvin_play/skip/play_control/recommend/radio)
  Phase 6 ⬜  清除 VoiceController forwarding stubs
"""
from __future__ import annotations

import logging

from discord.ext import commands

logger = logging.getLogger(__name__)


class MusicCog(commands.Cog):
    """音樂子系統（Strangler Fig 遷移中）。"""

    def __init__(self, bot):
        self.bot = bot
        # 跨切狀態 — VoiceController 透過 proxy property 讀寫這裡
        self.stream_mode: bool = False
        self.radio_mode: bool = False

    async def cog_load(self) -> None:
        logger.info("[MusicCog] Phase 1 骨架已載入（stream_mode/radio_mode proxy 就緒）")

    async def cog_unload(self) -> None:
        pass


async def setup(bot) -> None:
    await bot.add_cog(MusicCog(bot))
