"""PlaybackControlAgent — 播放控制的唯一責任人。

2026-07-30 職責分離：MusicAgentV2 對应的 control_skip/pause/resume/stop
移除，由此 agent 獨攀。

功能：
  - skip/stop/pause/resume 语音命令 → IntentBus 立刻回應（confidence=0.95）
  - 切歌 quick ack TTS（好，換 / 停了 / 暫停 / 繼續）
  - skip 連 2 不同 speaker 同一 url → 自動加 cover blacklist (D3 A 方案)

Phase 1 簡化: quick ack 用 既有 paid path。
Phase 1 ack LLM 失敗 fallback 用 hardcoded 字串。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_agents.constants import (
    MUSIC_SKIP_KW, MUSIC_STOP_KW, MUSIC_PAUSE_KW, MUSIC_RESUME_KW,
)
from intent_bus import IntentContext

logger = logging.getLogger(__name__)


# Quick ack text per intent（D2=A: TTS gen-on-demand；後續可 prerecord）
_ACK_TEXT = {
    "skip_track": "好，換",
    "stop_playback": "停了",
    "pause_playback": "暫停",
    "resume_playback": "繼續",
}

# 連續 skip 自動加黑名單 threshold (D3 A 方案)
# 註：「skip → blacklist」是 antipattern（見 memory: skip_signal_attribution.md）；
# 未來重構時把訊號送回 recommender，不是 hard ban 歌曲。
SKIP_BLACKLIST_THRESHOLD = 2  # 不同 speaker 數

# 議題 C (2026-05-27)：skip / stop / pause keyword 出現在 modal/question 之後 → chat。
# Filter 只看 prefix，避免 L19/L32 類 FP。
_CHAT_PREFIX_MARKERS = (
    # 推測 / 模糊（modal）
    "應該", "可能", "也許", "大概", "估計", "算了",
    # 疑問 / 反問
    "為什麼", "怎麼", "是不是", "有沒有", "該不該", "幹嘛",
)


class PlaybackControlAgent(DeclarativeIntentAgent):
    """語音播放控制 intent。

    三個 intent:
      skip_track     — 下一首/切歌/換歌/next/skip
      stop_playback  — 停止播放/停止/stop
      pause_playback — 暫停/pause

    mode_compatible = {"normal", "stream"}（遊戲模式不該誤觸發）。
    """

    name = "playback_control"
    mode_compatible = frozenset({"normal", "stream"})

    def __init__(self, controller):
        self.ctrl = controller
        self._intents_cache: list[IntentSchema] | None = None

    def declare_intents(self) -> list[IntentSchema]:
        if self._intents_cache is None:
            # 統一從 constants 取 keyword，與 MusicAgentV2 同源、不再各維各的
            skip_pat = "|".join(MUSIC_SKIP_KW)
            stop_pat = "|".join(MUSIC_STOP_KW)
            pause_pat = "|".join(MUSIC_PAUSE_KW)
            resume_pat = "|".join(MUSIC_RESUME_KW)
            self._intents_cache = [
                IntentSchema(
                    "skip_track", 0.95,
                    patterns=[f"({skip_pat})"],
                    reason_template="skip:{matched}",
                ),
                # pause 必須在 stop 之前：「暂停音樂」含「停音樂」，若 stop 先就會被誤判
                IntentSchema(
                    "pause_playback", 0.95,
                    patterns=[
                        f"({pause_pat})",
                        # 裸字「暫停」單獨成句才收（8/18 bot_main.log 實測 winner=none
                        # 真漏接）；不能做成 substring（MUSIC_PAUSE_KW 那種），因為
                        # 「他暫暫停他的冷氣」這類句子裡「暫停」只是路過的名詞，全句
                        # anchor 才能避開這類 FP。
                        r"^暫停[!！。\.，,]*$",
                    ],
                    reason_template="pause:{matched}",
                ),
                IntentSchema(
                    "stop_playback", 0.95,
                    patterns=[f"({stop_pat})"],
                    reason_template="stop:{matched}",
                ),
                IntentSchema(
                    "resume_playback", 0.95,
                    patterns=[f"({resume_pat})"],
                    reason_template="resume:{matched}",
                ),
            ]
        return self._intents_cache

    def gate(self, ctx: IntentContext) -> str | None:
        # stream_mode 検查已移除：music_cog._handle_voice_music_command 自己會判斷有沒歌在播。
        # PlaybackControlAgent 不需要在 gate 這層再裁剪一次（過先會導致 stream_mode
        # 剛歸位 False 時的指令被封鎖，發生失效 skip）。
        return None

    def post_match_filter(self, schema, slots, ctx) -> bool:
        """FP 防護：

        skip_track：語氣詞/否定詞出現在關鍵字前方 → 拒絕。
          - 底層由 is_short_skip_command 做位置+長度檢查（與 MusicAgentV2 同一邏輯）
          - 議題 C prefix filter（應該/為什麼/幾/怒啊… 出現在關鍵字前）→ chat，拒絕

        stop/pause/resume：僅議題 C prefix filter（同藏語關鍵字，但不需要位置檢查）。
        """
        text = ctx.query or ""

        if schema.name == "skip_track":
            from intent_agents.skip_intent import is_short_skip_command
            if not is_short_skip_command(text, MUSIC_SKIP_KW):
                return False
            # is_short_skip_command 已覆蓋位置+長度+否定，不需要再跟 prefix filter
            return True

        # stop/pause/resume：議題 C 的 chat marker prefix 檢查
        import re
        pos = -1
        for pat in schema.patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                pos = m.start()
                break
        if pos <= 0:
            return True  # keyword 在開頭或找不到 → 不擋
        prefix = text[:pos]
        for marker in _CHAT_PREFIX_MARKERS:
            if marker in prefix:
                return False
        return True

    def make_handler(
        self, schema: IntentSchema, slots: dict, ctx: IntentContext
    ) -> Callable[[], Awaitable[None]]:
        intent = schema.name
        speaker = ctx.speaker

        async def _handler() -> None:
            # Phase 1 M5 P5 簡化版: ack + action 並行
            ack_text = _ACK_TEXT.get(intent, "好")
            ack_task = asyncio.create_task(self._quick_ack(ack_text))
            action_task = asyncio.create_task(self._execute(intent, speaker, ctx.query))
            try:
                await asyncio.gather(ack_task, action_task)
            except Exception:
                logger.exception(f"[PlaybackControl] {intent} parallel failed")

        return _handler

    async def _quick_ack(self, text: str) -> None:
        """跑 quick ack TTS。LLM/TTS 失敗 → 既有 play_tts fallback chain 自己處理。"""
        try:
            play_tts = getattr(self.ctrl, "play_tts", None)
            if play_tts is None:
                logger.warning("[PlaybackControl] ctrl.play_tts 不存在，skip ack")
                return
            # already_in_channel=False：這是全新獨立的 ack，不是「正在講的話」的延續片段。
            # 講出切歌指令本身就是 barge-in，會把 _tts_interrupted 設 True；若這裡仍用
            # already_in_channel=True，[Interrupt Guard] 會看到這個殘留旗標把 ack 整句
            # 靜默丟棄（2026-07-30 實測：log 只見「中斷後跳過剩餘片段」，完全沒發聲）。
            await play_tts(text, already_in_channel=False)
        except Exception:
            logger.exception("[PlaybackControl] quick ack 失敗，靜默")

    async def _execute(self, intent: str, speaker: str, query: str) -> None:
        """委派 music_cog._safe_music_command 執行動作。

        協倹 music_cog 的單一實作出口：
          - dedup window、_record_song_skip、_current_song_skipped、_tail_dj_task取消都在內。
          - PlaybackControlAgent 除了 quick ack TTS + auto-blacklist，從此不再自己操作播放層。
        """
        intent_to_cmd = {
            "skip_track": "skip",
            "stop_playback": "stop",
            "pause_playback": "pause",
            "resume_playback": "resume",
        }
        cmd = intent_to_cmd.get(intent)
        if cmd is None:
            logger.warning(f"[PlaybackControl] 未知 intent={intent}")
            return

        # skip 额外處理：auto-blacklist（D3 A 方案）
        if cmd == "skip":
            current = getattr(self.ctrl, "_current_stream_info", None)
            current_url = current.get("url") if current else None
            if current_url:
                tracker = getattr(self.ctrl, "_consecutive_skips_by_url", None)
                if tracker is None:
                    self.ctrl._consecutive_skips_by_url = {}
                    tracker = self.ctrl._consecutive_skips_by_url
                spk_set = tracker.setdefault(current_url, set())
                spk_set.add(speaker)
                if len(spk_set) >= SKIP_BLACKLIST_THRESHOLD:
                    self._add_to_blacklist(current_url, current.get("title", ""), spk_set)
                    tracker.pop(current_url, None)

        # 委派給 music_cog（包含 dedup、_record_song_skip、_tail_dj_task 取消等）
        try:
            bot = getattr(self.ctrl, "bot", None)
            mc = bot.cogs.get("MusicCog") if bot else None
            if mc is None:
                logger.warning(f"[PlaybackControl] MusicCog 未載入，無法執行 {cmd}")
                return
            await mc._safe_music_command(speaker, query, cmd)
        except Exception:
            logger.exception(f"[PlaybackControl] 委派 {cmd} 失敗")

    def _voice_client(self):
        """從 ctrl 取當前 active voice_client；無則 None。"""
        bot = getattr(self.ctrl, "bot", None)
        if bot is None:
            return None
        for vc in getattr(bot, "voice_clients", []):
            if getattr(vc, "is_connected", lambda: False)():
                return vc
        return None

    def _add_to_blacklist(self, url: str, title: str, speakers: set[str]) -> None:
        """Skip 連 2 不同人 → 加進 cover quality blacklist。"""
        try:
            from track_quality import CoverBlacklist
            bl = getattr(self.ctrl, "_cover_blacklist", None) or CoverBlacklist.shared()
            reason = f"auto: skipped by {len(speakers)} users ({','.join(sorted(speakers))[:60]})"
            bl.add(url, reason=reason)
            self.ctrl._cover_blacklist = bl  # cache 回 ctrl
            logger.info(f"🚫 [PlaybackControl] 自動加入黑名單: {title} (url={url}) reason={reason}")
        except Exception:
            logger.exception("[PlaybackControl] 加黑名單失敗")
