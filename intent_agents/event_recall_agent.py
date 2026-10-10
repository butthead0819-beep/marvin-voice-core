"""EventRecallAgent — 「我上個月去哪裡露營」「showay 什麼時候要出國」問答。

事件記憶 Phase 1（docs/designs/event-memory.md）：session_summarizer 每窗抽事件存進
event_memory.EventStore；這裡只負責查——先用 present_speakers 隱私濾網過濾出
在場者全部 ⊆ heard_by 的事件，再讓 LLM 從候選清單裡挑 id，答案文字完全是 DB
欄位組句（render_answer），不讓 LLM 自由生成事實。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from event_memory import (
    RETENTION_DAYS,
    SELECT_CANDIDATE_LIMIT,
    SELECT_SYSTEM_PROMPT,
    NOT_REMEMBER,
    EventStore,
    build_select_user_prompt,
    parse_ids,
    render_answer,
    visible_events,
)
from intent_agents.base import DeclarativeIntentAgent, IntentSchema, is_audio_rescue
from intent_bus import IntentContext
from speaker_isolation import present_speakers

logger = logging.getLogger(__name__)


class EventRecallAgent(DeclarativeIntentAgent):
    name = "event_recall"
    mode_compatible = frozenset({"normal", "stream"})

    def __init__(self, controller, store=None):
        self.ctrl = controller
        self._store = store
        self._cache = None

    def declare_intents(self) -> list[IntentSchema]:
        if self._cache is None:
            self._cache = [
                IntentSchema(
                    "event_recall", 0.88,
                    patterns=[
                        r"(?:上個?月|上次|上禮拜|上週|前陣子|之前|那次)[^，。！？?]{0,12}(?:去|到)了?(?:哪裡|哪邊|哪)",
                        r"(?:什麼時候|哪天|幾號)[^，。！？?]{0,4}(?:要)?(?:去|出發|出國|回來)",
                    ],
                    required_slots=[],
                    reason_template="event_recall",
                    manifest_description=(
                        "使用者問自己或在場朋友『過去或計畫中的生活行程』（例：我上個月去哪裡露營、showay 上次出國去哪、"
                        "狗與露什麼時候要回老家、我們上次聚餐在哪）。答案來自 Marvin 聽過的聊天紀錄。"
                        "不是查網路資訊、不是待辦清單、不是問歌曲。"
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
            try:
                store = self._store or EventStore()
                self._store = store
                guild_id = int(os.environ.get("GUILD_ID") or "0")
                now = time.time()
                present = present_speakers(ctx.speaker, self.ctrl.get_online_members())
                events = await asyncio.to_thread(store.list_guild, guild_id, now - RETENTION_DAYS * 86400)
                visible = visible_events(events, present)[:SELECT_CANDIDATE_LIMIT]
                text = NOT_REMEMBER
                if visible:
                    try:
                        content = await asyncio.wait_for(
                            self.ctrl.bot.router._call_llm(
                                SELECT_SYSTEM_PROMPT,
                                build_select_user_prompt(ctx.query, ctx.speaker, visible),
                                is_json=True, tier="simple", temperature=0.0, purpose="event_recall",
                            ),
                            timeout=12.0,
                        )
                    except Exception as e:
                        logger.warning(f"🗓️ [EventRecall] 選事件 LLM 失敗: {e}")
                        content = ""
                    ids = parse_ids(content or "", {e["id"] for e in visible})
                    if ids:
                        ev = next(e for e in visible if e["id"] == ids[0])
                        text = render_answer(ev, ctx.speaker)
                logger.info(f"🗓️ [EventRecall] {ctx.speaker} 問「{ctx.query}」 可見 {len(visible)} 筆 → {text}")
                await self.ctrl.play_tts(text, already_in_channel=True)
            except Exception:
                logger.exception("[EventRecall] 失敗")

        return _answer
