"""SongFeedbackAgent — 控制卡按鈕語音化：誤點刪除 / 喜歡。

2026-10-13 showay 喚醒說「馬文，誤點刪除」（照控制卡按鈕名稱唸），被
`_query_quality_gate` 以 too_short 擋掉（cleaner 改成「誤刪除」3 字）；第二次
STT 成「馬文 5. 刪除」又被擋。就算放行，語音也沒有任何路徑能觸發誤點刪除——
這功能只存在於按鈕。原則：已有的按鈕功能都要能用講的觸發。

兩個 intent：
  misclick_delete (0.92) — 把正在播的這首從記憶抹除、永久排除、跳下一首
  like_song       (0.90) — 幫正在播的這首按讚

mode_compatible = {"normal", "stream"}。
Gate：stream_mode + _current_stream_info 都要存在（同 NowPlayingAgent）。

`erase_misclick()` 是模組層 helper，控制卡按鈕（cogs/voice_views.py
misclick_button）與這顆 agent 共用同一份「抹除」邏輯，避免兩處各自維護一份
undo_play + record_skipped_video_id 的組合。
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_bus import IntentContext

logger = logging.getLogger(__name__)


def erase_misclick(ctrl) -> str | None:
    """把正在播的這首從點歌記憶抹除＋永久排除。不做 skip（由呼叫端各自處理）。

    回傳被抹除的歌名；沒有正在播放的歌（非 stream_mode 或無 _current_stream_info）
    回 None。
    """
    cur = getattr(ctrl, "_current_stream_info", None)
    if not getattr(ctrl, "stream_mode", False) or not cur:
        return None
    mm = getattr(getattr(ctrl, "bot", None), "music_memory", None)
    if mm is not None:
        try:
            mm.undo_play(cur)
            url = cur.get("webpage_url") or cur.get("url") or ""
            if url:
                mm.record_skipped_video_id(url)
        except Exception:
            logger.exception("[Misclick] 抹除失敗")
    title = cur.get("title", "這首")
    logger.info(f"[Misclick] 抹除＋永久排除：{title}")
    return title


class SongFeedbackAgent(DeclarativeIntentAgent):
    name = "song_feedback"
    mode_compatible = frozenset({"normal", "stream"})

    def __init__(self, controller):
        self.ctrl = controller
        self._intents_cache: list[IntentSchema] | None = None

    def declare_intents(self) -> list[IntentSchema]:
        if self._intents_cache is None:
            self._intents_cache = [
                IntentSchema(
                    "misclick_delete", 0.92,
                    patterns=[
                        r"^(?!.*(?:喜好|口味|興趣|喜歡|記得|記住|待播|佇列|清單|歌單"
                        r"|行事曆|提醒|記憶)).*?(?:誤點|刪除|刪掉|刪了|抹掉|抹除)",
                    ],
                    reason_template="misclick:{matched}",
                    manifest_description=(
                        "使用者說正在播的這首是誤點/不該播，要把它從點歌記憶抹除、"
                        "永久不再自動推薦，並立刻跳下一首（等同控制卡「🙈 誤點刪除」"
                        "按鈕）。不是一般的換下一首、不是清空待播。"
                    ),
                ),
                IntentSchema(
                    "like_song", 0.90,
                    patterns=[
                        r"^(?!.*(?:不|沒|別|取消)).*?(?:喜歡這首|愛這首|按讚|按個讚|"
                        r"點讚|給(?:這首|它|他)?(?:一個|個)?讚|收藏這首)",
                    ],
                    reason_template="like:{matched}",
                    manifest_description=(
                        "使用者喜歡正在播的這首歌，要幫他按讚記下來（等同控制卡"
                        "「❤️ 喜歡」按鈕）。不是點歌、不是問歌名。"
                    ),
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
        if schema.name == "misclick_delete":
            return self._make_misclick_handler(speaker)
        return self._make_like_handler(speaker)

    def _make_misclick_handler(self, speaker: str) -> Callable[[], Awaitable[None]]:
        async def _handler() -> None:
            ch = getattr(self.ctrl, "active_text_channel", None)
            title = erase_misclick(self.ctrl)
            if title is None:
                if ch is not None:
                    try:
                        await ch.send("現在沒有正在播放的歌可以抹除。")
                    except Exception:
                        logger.exception("[Misclick] 無歌可抹除提示失敗")
                return
            bot = getattr(self.ctrl, "bot", None)
            mc = getattr(bot, "cogs", {}).get("MusicCog") if bot is not None else None
            if mc is not None and hasattr(mc, "_safe_music_command"):
                try:
                    await mc._safe_music_command(speaker, "", "skip")
                except Exception:
                    logger.exception("[Misclick] skip 失敗")
            if ch is not None:
                try:
                    await ch.send(f"🙈 已把「{title[:40]}」從記憶抹除，之後不會再自動點。")
                except Exception:
                    logger.exception("[Misclick] 完成提示送出失敗")

        return _handler

    def _make_like_handler(self, speaker: str) -> Callable[[], Awaitable[None]]:
        async def _handler() -> None:
            info = self.ctrl._current_stream_info
            mm = self.ctrl.bot.music_memory
            title = (info.get("title") or "這首")[:40]
            ch = getattr(self.ctrl, "active_text_channel", None)
            liked = False
            if speaker in mm.get_likers(info):
                text = f"❤️ {speaker} 已經喜歡「{title}」了，記著呢。"
            else:
                state = mm.toggle_like(info, speaker)
                if state is True:
                    text = f"❤️ 記下了，{speaker} 喜歡「{title}」——之後會更常推這類的歌給你。"
                    liked = True
                elif state is None:
                    text = "這首還沒被記錄，等它播一下再試。"
                else:
                    text = f"💔 取消了對「{title}」的喜歡。"
            if ch is not None:
                try:
                    await ch.send(text)
                except Exception:
                    logger.exception("[Like] 文字提示送出失敗")
            if liked:
                try:
                    play_tts = getattr(self.ctrl, "play_tts", None)
                    if play_tts is not None:
                        await play_tts("好，記下了", already_in_channel=True)
                except Exception:
                    logger.exception("[Like] ack failed")

        return _handler
