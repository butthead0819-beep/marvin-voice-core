"""NowPlayingAgent — 「現在播的是什麼」資訊查詢 intent.

對應 2026-05-27 議題 E #3：L36「現在播的是什麼歌」走 wake 路徑落到 bus 卻無人接，
both-dense-zero。

voice_controller 既有 `_MUSIC_INFO_RE` no-wake 直達路徑處理「無 wake」case；
本 agent 填 wake gap。patterns 對齊既有 regex 三組（單一語意源）。

confidence 0.90，1 個 intent：now_playing。
mode_compatible = {"normal", "stream"}。
Gate：stream_mode + _current_stream_info 都要存在。

Handler 直接呼叫 ctrl._handle_music_info_query(speaker, query)；既有實作已處理
title/uploader/requested_by 組裝 + 送 text channel。
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_bus import IntentContext

logger = logging.getLogger(__name__)


class NowPlayingAgent(DeclarativeIntentAgent):
    name = "now_playing"
    mode_compatible = frozenset({"normal", "stream"})

    def __init__(self, controller):
        self.ctrl = controller
        self._intents_cache: list[IntentSchema] | None = None

    def declare_intents(self) -> list[IntentSchema]:
        if self._intents_cache is None:
            self._intents_cache = [
                IntentSchema(
                    "now_playing", 0.90,
                    patterns=[
                        # 「這首…」系列（叫什麼/是什麼/是誰/叫做/的名字/哪首/叫/叫啥）
                        r"這首(?:歌|曲)?(?:叫什麼|是什麼|是誰|叫做|的名字|哪首|叫啥|叫)",
                        # 「(現在|剛才|正在)(播|放|唱)的」系列（L36 原案）
                        r"(?:現在|剛才|正在)(?:播|放|唱)的",
                        # 「歌名/歌手/藝人/誰唱/誰寫」系列
                        r"(?:歌名|歌手|藝人|誰唱|誰寫)(?:是什麼|叫什麼|是誰|叫)?",
                    ],
                    reason_template="now_playing:{matched}",
                ),
            ]
        return self._intents_cache

    def gate(self, ctx: IntentContext) -> str | None:
        if not getattr(self.ctrl, "stream_mode", False):
            return "stream_not_active"
        if not getattr(self.ctrl, "_current_stream_info", None):
            return "no_current_song"
        return None

    def make_handler(
        self, schema: IntentSchema, slots: dict, ctx: IntentContext
    ) -> Callable[[], Awaitable[None]]:
        speaker = ctx.speaker
        query = ctx.query or ""

        async def _handler() -> None:
            handle = getattr(self.ctrl, "_handle_music_info_query", None)
            if handle is None:
                logger.warning("[NowPlaying] ctrl 沒裝 _handle_music_info_query，跳過")
                return
            try:
                await handle(speaker, query)
            except Exception:
                logger.exception("[NowPlaying] _handle_music_info_query failed")

        return _handler
