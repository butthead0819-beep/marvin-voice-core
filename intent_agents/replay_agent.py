"""ReplayAgent — 重播歌曲 intent.

對應 2026-05-27 議題 E #2：L44「重播這一首」是 both-dense-zero 的有效 intent。
2026-09-28 事故修正：使用者講「重播一次<歌名>」時原本無視歌名、且 Plan12
混音器下 vc.stop_playing() 停不掉，導致播完當下這首才重播成同一首歌。

confidence 0.90，1 個 intent：replay。

mode_compatible = {"normal", "stream"}。
Gate：
  - stream_mode 必須開
  - _current_stream_info 必須存在

Handler 用 extract_title_query() 從 query 抽出歌名（可能是空字串）：
  1. 沒抽到歌名，或歌名對到「當下這首」→ 從頭重播當下這首（停歌＋插回佇列最前面）。
  2. 講了別首歌名 → 就是點歌（「重播X」＝想聽X）：對到 stream_history 最近播過的
     就用它的 webpage_url 點（省搜尋），否則用歌名點；走既有 "play"，不停歌、不唸 ack。

停歌方式（_stop_current_playback）：
  - 不可呼叫既有的 "skip" 指令 —— skip 會把當下這首寫進永久排除清單，
    重播不是嫌棄這首歌。
  - Plan12 本地混音器下 vc.stop_playing()/vc.stop() 停不掉（歌會播完），
    改呼叫 mixer.clear_music()。
  - 非 Plan12 沿用舊的 vc.stop_playing()/vc.stop() 迴圈。
"""
from __future__ import annotations

import logging
import re
from typing import Awaitable, Callable

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_bus import IntentContext
from song_name_clean import clean_title_regex

logger = logging.getLogger(__name__)

_REPLAY_PATTERNS = [
    # 重播 / 重播這(一)?首
    r"重播",
    # 再(放|播|聽)一次
    r"再\s*(放|播|聽)\s*一?次",
    # 倒回 / 倒帶
    r"(倒回|倒帶)",
    # 從頭(再)?(播)?
    r"從頭(\s*再)?(\s*播)?",
    # replay / play again
    r"replay",
    r"play\s*again",
]

_WAKE_WORDS = ["馬文", "marvin"]

# 長的排前面，避免先剝短的把長的截斷
_FILLERS = [
# 不收「歌/的/放/播」單字：歌名常以它們開頭結尾（情歌、山歌），剝掉會吃到歌名
    "剛剛那首", "剛才那首", "這一首歌", "那一首歌", "這首歌", "那首歌", "這一首", "那一首",
    "一次", "一遍", "這首", "那首", "剛剛", "剛才",
    "給我", "幫我", "一下", "播放",
    "請", "吧", "啦", "啊", "嗎", "再",
]

_PUNCT_AND_SPACE = " \t　，。！？,.!?、「」『』《》"


def extract_title_query(query: str) -> str:
    """從 replay query 裡抽出歌名（找不到回空字串）。"""
    text = query
    for w in _WAKE_WORDS:
        text = re.sub(w, "", text, flags=re.IGNORECASE)
    for pat in _REPLAY_PATTERNS:
        text = re.sub(pat, "", text, flags=re.IGNORECASE)

    changed = True
    while changed:
        changed = False
        stripped = text.strip(_PUNCT_AND_SPACE)
        if stripped != text:
            text = stripped
            changed = True
        for filler in _FILLERS:
            if text.startswith(filler):
                text = text[len(filler):]
                changed = True
            if text.endswith(filler):
                text = text[: len(text) - len(filler)]
                changed = True
        text = text.strip(_PUNCT_AND_SPACE)

    if len(text) < 2:
        return ""
    return text


def _norm(s: str) -> str:
    s = s.lower()
    for ch in _PUNCT_AND_SPACE:
        s = s.replace(ch, "")
    return s


def title_matches(title_query: str, song_title: str) -> bool:
    """title_query 是否對得上 song_title（或其清理後的版本）。"""
    nq = _norm(title_query)
    if not nq:
        return False
    if nq in _norm(song_title):
        return True
    if nq in _norm(clean_title_regex(song_title)):
        return True
    return False


class ReplayAgent(DeclarativeIntentAgent):
    name = "replay"
    mode_compatible = frozenset({"normal", "stream"})

    def __init__(self, controller):
        self.ctrl = controller
        self._intents_cache: list[IntentSchema] | None = None

    def declare_intents(self) -> list[IntentSchema]:
        if self._intents_cache is None:
            self._intents_cache = [
                IntentSchema(
                    "replay", 0.90,
                    patterns=_REPLAY_PATTERNS,
                    reason_template="replay:{matched}",
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
        query = ctx.query

        async def _handler() -> None:
            title_q = extract_title_query(query)
            current = self.ctrl._current_stream_info

            if title_q and not title_matches(title_q, current.get("title", "")):
                # 講了別首歌名＝點歌（使用者 2026-09-28：重播X 就是想聽X）。最近播過的
                # 直接用它的 YouTube 網址點，省掉搜尋；不切當下這首，照一般點歌排隊。
                hit = self._find_in_history(title_q, current)
                play_query = (hit or {}).get("webpage_url") or title_q
                try:
                    await self.ctrl._safe_music_command(speaker, play_query, "play")
                    logger.info(
                        f"[Replay] {speaker} '{title_q}' → 轉點歌"
                        f"（{'最近播過' if hit else '搜尋'}：{play_query}）"
                    )
                except Exception:
                    logger.exception("[Replay] fallback play failed")
                return

            target = current
            item = dict(target)
            item.pop("_dj_played_in_tail", None)
            try:
                self.ctrl.stream_queue.insert(0, item)
            except Exception:
                logger.exception("[Replay] enqueue failed")
            try:
                self._stop_current_playback()
            except Exception:
                logger.exception("[Replay] stop playback failed")
            await self._ack()
            logger.info(f"[Replay] triggered by {speaker} → {item.get('title', '?')}")

        return _handler

    def _find_in_history(self, title_q: str, current: dict) -> dict | None:
        history = getattr(self.ctrl, "stream_history", None)
        if not isinstance(history, list):
            return None
        for info in reversed(history[-20:]):
            if info is current:
                continue
            if title_matches(title_q, info.get("title", "")):
                return info
        return None

    def _stop_current_playback(self) -> None:
        bot = getattr(self.ctrl, "bot", None)
        try:
            mc = bot.cogs.get("MusicCog") if bot is not None else None
        except Exception:
            mc = None
        if mc is not None:
            try:
                # 不能記成 skip：不然這首歌會被 _record_song_skip() 寫進
                # 永久排除清單，重播不是嫌棄這首歌。這個旗標只是讓
                # stream loop 別把提早結束的播放當成 403 斷線去重試。
                mc._current_song_skipped = True
            except Exception:
                logger.exception("[Replay] set _current_song_skipped failed")
            try:
                t = getattr(mc, "_tail_dj_task", None)
                if t is not None and not t.done():
                    t.cancel()
                    mc._tail_dj_task = None
            except Exception:
                logger.exception("[Replay] cancel tail dj task failed")

        mixer = getattr(self.ctrl, "_mixer", None)
        if getattr(self.ctrl, "_plan12", False) and mixer is not None:
            try:
                mixer.clear_music()
            except Exception:
                logger.exception("[Replay] mixer.clear_music failed")
            return

        if bot is None:
            return
        for vc in getattr(bot, "voice_clients", []):
            if not getattr(vc, "is_connected", lambda: False)():
                continue
            if hasattr(vc, "stop_playing"):
                vc.stop_playing()
            elif hasattr(vc, "stop"):
                vc.stop()
            return

    async def _ack(self) -> None:
        try:
            play_tts = getattr(self.ctrl, "play_tts", None)
            if play_tts is None:
                return
            await play_tts("好，再放一次", already_in_channel=True)
        except Exception:
            logger.exception("[Replay] ack failed")
