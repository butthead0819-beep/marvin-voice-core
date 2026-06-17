"""MusicCog — 音樂子系統（從 VoiceController 抽離中）。

Phase 3 (電台狀態)：持有 radio subsystem 全部狀態；音樂邏輯仍在 VC，逐步遷移。

遷移進度：
  Phase 1 ✅  骨架 + stream_mode/radio_mode proxy
  Phase 2 ✅  stream subsystem state proxy (stream_queue, _current_stream_info, …)
  Phase 3 ✅  radio subsystem state proxy (radio_task, radio_paused, …)
  Phase 4 ⬜  _auto_recommend + song metadata + DJ
  Phase 5 ⬜  slash commands (marvin_play/skip/play_control/recommend/radio)
  Phase 6 ⬜  清除 VoiceController forwarding stubs
"""
from __future__ import annotations

import logging
from typing import Optional

from discord.ext import commands

logger = logging.getLogger(__name__)


class MusicCog(commands.Cog):
    """音樂子系統（Strangler Fig 遷移中）。"""

    def __init__(self, bot):
        self.bot = bot
        # 跨切狀態 — VoiceController 透過 proxy property 讀寫這裡
        self.stream_mode: bool = False
        self.radio_mode: bool = False

        # 🎵 [Phase 2] Stream subsystem state (proxied from VoiceController)
        self.stream_volume: float = 0.10
        self._stream_play_gen: int = 0
        self._current_stream_url: Optional[str] = None
        self._stream_norm_gain: dict = {}   # url → 每首響度正規化常數增益
        self._last_user_song_seed: Optional[str] = None
        self.stream_queue: list = []        # list of {title, uploader, url, …}
        self.stream_task = None
        self._current_stream_info = None
        self.stream_history: list = []      # 已播過的歌曲（用於上一首）
        self.stream_paused: bool = False
        self._current_lyrics: Optional[str] = None
        self._current_stream_comment: Optional[str] = None
        self._active_control_view = None

        # 📻 [Phase 3] Radio subsystem state (proxied from VoiceController)
        self.radio_task = None
        self.radio_volume: float = 0.10
        self._radio_song_list: list = []
        self._radio_source = None
        self._radio_fade_task = None
        self.radio_paused: bool = False

    async def cog_load(self) -> None:
        logger.info("[MusicCog] Phase 3 已載入（stream + radio state proxy 就緒）")

    async def cog_unload(self) -> None:
        pass


async def setup(bot) -> None:
    await bot.add_cog(MusicCog(bot))
