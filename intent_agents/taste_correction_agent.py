"""TasteCorrectionAgent — 使用者口頭勘誤/查詢 suki_memory 記錯的口味。

背景：使用者口味存在 suki_memory 的 `taste`（score ≥3 投影成 likes），但目前沒有
任何方式讓使用者更正記錯的喜好。這是刪資料的動作，gate 用比一般 agent 更嚴的
wake_intent 門檻（0.65，對齊 base.py PHONETIC_WAKE_MIN_INTENT 慣例）確保是對
Marvin 說的，不是誤觸發。

2 個 intent：
  - taste_forget（0.88）：「把X從我的喜好拿掉」「我沒有喜歡X」「忘掉我喜歡X」
    只更正「喜歡」側（taste score > 0），不動 taboos（敏感標記不該被這條路徑清掉），
    也不能誤刪「討厭」側（score < 0）——「我不喜歡香菜」是正當的討厭表達，patterns
    刻意不含「不喜歡」，只含「沒有/沒/不是喜歡」。
  - taste_query（0.85）：「你記得我喜歡什麼」「我的喜好有哪些」

item 比對允許子字串雙向命中（taste 記的是「周杰倫的歌」，使用者說「周杰倫」也該中），
但 key 長度 <2 時不做子字串比對，避免單字過度寬鬆命中。
"""
from __future__ import annotations

import logging

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_bus import IntentContext

logger = logging.getLogger(__name__)

_TRAILING_PARTICLES = "啦呢喔耶欸啊嘛喲哦了吧嗎哈"

LOW_WAKE_THRESHOLD = 0.65


def _clean_item(raw: str) -> str:
    item = (raw or "").strip()
    while item and item[-1] in _TRAILING_PARTICLES:
        item = item[:-1]
    if item.startswith("的"):
        item = item[1:]
    return item.strip()


class TasteCorrectionAgent(DeclarativeIntentAgent):
    name = "taste_correction"
    mode_compatible = frozenset({"normal", "stream"})

    def __init__(self, controller):
        self.ctrl = controller
        self._intents_cache: list[IntentSchema] | None = None

    def _memory(self):
        return getattr(getattr(getattr(self.ctrl, "bot", None), "router", None), "memory", None)

    def gate(self, ctx: IntentContext) -> str | None:
        if ctx.wake_intent is not None and ctx.wake_intent < LOW_WAKE_THRESHOLD:
            return "low_wake"
        return None

    def declare_intents(self) -> list[IntentSchema]:
        if self._intents_cache is None:
            self._intents_cache = [
                IntentSchema(
                    "taste_forget", 0.88,
                    patterns=[
                        r"把(?P<item>[^，。！？\s]{1,12}?)從我的(?:喜好|喜歡|口味|興趣)(?:清單)?(?:裡面|裡|中)?(?:拿掉|刪掉|移除|刪除|去掉)",
                        r"我(?:其實)?(?:沒有|沒|不是)(?:很|特別)?(?:喜歡|愛)(?P<item>[^，。！？\s]{1,12})",
                        r"(?:忘掉|忘記|別再記得?|不要再記得?)我(?:喜歡|愛)(?P<item>[^，。！？\s]{1,12})",
                    ],
                    required_slots=["item"],
                    reason_template="taste_forget:{item}",
                    manifest_description="使用者要 Marvin 忘掉/更正某項被記錯的喜好（如「把X從我的喜好拿掉」「我沒有喜歡X」）",
                ),
                IntentSchema(
                    "taste_query", 0.85,
                    patterns=[
                        r"你(?:記得|知道)我(?:喜歡|愛)(?:什麼|啥|哪些)",
                        r"我的(?:喜好|口味)(?:有哪些|是什麼)",
                    ],
                    reason_template="taste_query",
                    manifest_description="使用者問 Marvin 記得自己喜歡什麼",
                ),
            ]
        return self._intents_cache

    def post_match_filter(self, schema: IntentSchema, slots: dict, ctx: IntentContext) -> bool:
        if schema.name == "taste_forget":
            item = _clean_item(slots.get("item", ""))
            slots["item"] = item
            if len(item) < 2:
                return False
            # 「我沒有很喜歡這首歌」是對當下歌曲的回饋，不是勘誤記憶——指代詞開頭不接
            if item[0] in "這那":
                return False
        return True

    def make_handler(self, schema: IntentSchema, slots: dict, ctx: IntentContext):
        if schema.name == "taste_forget":
            return self._make_forget_handler(slots.get("item", ""), ctx)
        if schema.name == "taste_query":
            return self._make_query_handler(ctx)
        return self._noop

    def _make_forget_handler(self, item: str, ctx: IntentContext):
        async def _handler() -> None:
            try:
                mem = self._memory()
                if mem is None:
                    logger.warning("[TasteCorrection] 記憶模組不存在，無法勘誤喜好")
                    return
                from suki_memory import is_pseudo_player

                speaker = ctx.speaker
                if is_pseudo_player(speaker):
                    return
                if not mem.has_player(speaker):
                    await self.ctrl.speak("我本來就沒記得你喜歡什麼。")
                    return

                player = mem.get_player_memory(speaker)
                taste = player.get("taste") or {}
                taboos = set(player.get("taboos") or [])

                matches = [
                    key for key, value in taste.items()
                    if isinstance(value, dict)
                    and value.get("score", 0) > 0
                    and key not in taboos
                    and (key == item or (len(key) >= 2 and (item in key or key in item)))
                ]

                if not matches:
                    await self.ctrl.speak(f"我沒有記得你喜歡「{item}」。")
                    return

                for key in matches:
                    mem.remove_taste_item(speaker, key)
                logger.info(f"[TasteCorrection] {speaker} 移除喜好: {matches}")
                await self.ctrl.speak(f"好，已經把「{'、'.join(matches[:3])}」從你的喜好拿掉了。")
            except Exception:
                logger.exception("[TasteCorrection] taste_forget handler 失敗")

        return _handler

    def _make_query_handler(self, ctx: IntentContext):
        async def _handler() -> None:
            try:
                mem = self._memory()
                if mem is None:
                    logger.warning("[TasteCorrection] 記憶模組不存在，無法查詢喜好")
                    return
                from suki_memory import is_pseudo_player

                speaker = ctx.speaker
                if is_pseudo_player(speaker):
                    return
                if not mem.has_player(speaker):
                    await self.ctrl.speak("我還沒記下你喜歡什麼。")
                    return

                likes = mem.get_player_memory(speaker).get("likes") or []
                if not likes:
                    await self.ctrl.speak("我還沒記下你喜歡什麼。")
                    return

                top = likes[:5]
                await self.ctrl.speak(
                    f"我記得你喜歡{'、'.join(top)}。記錯的話跟我說「把某某從我的喜好拿掉」。"
                )
            except Exception:
                logger.exception("[TasteCorrection] taste_query handler 失敗")

        return _handler
