"""MusicAgent v2 — declarative version (validation target for new arch).

Behavior parity goal vs v1：
  - 同 query → 同 Bid(name, confidence, missing_slots)
  - reason 格式可微調，但要可解析
  - dense bid 0.0 + reason 是 v2 新增（v1 是 None），驗證 negative space 表達

Schema priority order（保持與 v1 一致）：
  1. control_skip / control_pause / control_resume / control_stop (0.95)
  2. strong_play (0.95)
  3. weak_play_with_marker (0.80)
  4. weak_play_long_string (0.55, missing_slots=["song_title"])
"""
from __future__ import annotations

import re
from collections import Counter

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_bus import IntentContext


# Music intent markers — same as v1
_MUSIC_INTENT_MARKERS = ("的", "歌", "曲", "音樂", "mv", "ost", "歌詞", "歌手",
                         "一首", "那首", "這首")

# UI/system words that should NOT trigger weak_play (same blocklist as v1)
_NON_MUSIC_TARGETS = frozenset([
    "控制", "清單", "列表", "設定", "選項", "畫面", "頁面", "音量", "狀態",
    "這個", "那個", "它", "他", "她", "東西", "什麼",
])

# Hallucination guard params
_REPETITION_WINDOW = 3
_REPETITION_THRESHOLD = 3
_REPETITION_MIN_LEN = 6
_REPETITION_MAX_LEN = 200


def _kw_alt(kws) -> str:
    """Build regex alternation from keyword list, longest first to avoid prefix shadowing."""
    return "|".join(re.escape(kw) for kw in sorted(kws, key=len, reverse=True))


def _looks_repetitive(query: str) -> bool:
    q_len = len(query)
    if q_len < _REPETITION_MIN_LEN or q_len > _REPETITION_MAX_LEN:
        return False
    windows = [query[i:i + _REPETITION_WINDOW]
               for i in range(q_len - _REPETITION_WINDOW + 1)]
    if not windows:
        return False
    top_count = Counter(windows).most_common(1)[0][1]
    return top_count >= _REPETITION_THRESHOLD


class MusicAgentV2(DeclarativeIntentAgent):
    name = "music"
    # 音樂 agent 在正常對話與串流播放期間都活著；遊戲模式下不該誤觸發
    mode_compatible = frozenset({"normal", "stream"})
    LOW_WAKE_THRESHOLD = 0.65

    def __init__(self, controller):
        self.ctrl = controller
        self._intents_cache: list[IntentSchema] | None = None

    # ── Gates ────────────────────────────────────────────────────────────────

    def gate(self, ctx: IntentContext) -> str | None:
        if ctx.wake_intent is not None and ctx.wake_intent < self.LOW_WAKE_THRESHOLD:
            return "low_wake_intent"
        if _looks_repetitive(ctx.query or ""):
            return "repetitive_hallucination"
        return None

    # ── Intent schemas ───────────────────────────────────────────────────────

    def declare_intents(self) -> list[IntentSchema]:
        if self._intents_cache is not None:
            return self._intents_cache

        ctrl = self.ctrl
        skip_kws = _kw_alt(ctrl._MUSIC_SKIP_KW)
        pause_kws = _kw_alt(ctrl._MUSIC_PAUSE_KW)
        resume_kws = _kw_alt(ctrl._MUSIC_RESUME_KW)
        stop_kws = _kw_alt(ctrl._MUSIC_STOP_KW)
        strong_kws = _kw_alt(ctrl._STRONG_PLAY_KW)
        weak_kws = _kw_alt(ctrl._WEAK_PLAY_KW)
        markers = _kw_alt(_MUSIC_INTENT_MARKERS)

        self._intents_cache = [
            # Control intents — priority order matches v1 (skip → pause → resume → stop)
            IntentSchema("control_skip", 0.95,
                         patterns=[f"(?P<kw>{skip_kws})"],
                         reason_template="control:skip"),
            IntentSchema("control_pause", 0.95,
                         patterns=[f"(?P<kw>{pause_kws})"],
                         reason_template="control:pause"),
            IntentSchema("control_resume", 0.95,
                         patterns=[f"(?P<kw>{resume_kws})"],
                         reason_template="control:resume"),
            IntentSchema("control_stop", 0.95,
                         patterns=[f"(?P<kw>{stop_kws})"],
                         reason_template="control:stop"),
            # Strong play — kw 命中即 0.95
            IntentSchema("strong_play", 0.95,
                         patterns=[f"(?P<kw>{strong_kws})"],
                         reason_template="strong_play:{kw}"),
            # Weak play + marker（marker 在 query 任意處出現）
            IntentSchema("weak_play_with_marker", 0.80,
                         patterns=[f"(?P<kw>{weak_kws})(?=.*(?:{markers}))"],
                         reason_template="weak_play+marker:{kw}"),
            # Weak play + 後續 ≥2 字（artist-only fallback）
            # named group 'target' = kw 後的內容；post_match_filter 檢查不在 blocklist
            IntentSchema("weak_play_long_string", 0.55,
                         patterns=[
                             f"(?P<kw>{weak_kws})[，,、！!？?。. ]*(?P<target>\\S{{2,}})"
                         ],
                         required_slots=["song_title"],
                         reason_template="weak_play_only:{kw}->{target}"),
        ]
        return self._intents_cache

    # ── Post-match filter (NON_MUSIC_TARGETS blocklist) ──────────────────────

    def post_match_filter(self, schema, slots, ctx):
        if schema.name != "weak_play_long_string":
            return True
        target = slots.get("target", "").strip("，,、！!？?。. ")
        # v1 也只看 target 開頭 20 字
        if target[:20] in _NON_MUSIC_TARGETS:
            return False
        return True

    # ── Handler wiring ───────────────────────────────────────────────────────

    def make_handler(self, schema, slots, ctx):
        # Map schema → controller call (parity with v1 handlers)
        if schema.name.startswith("control_"):
            cmd = schema.name.split("_", 1)[1]
            async def _control():
                await self.ctrl._safe_music_command(ctx.speaker, ctx.query, cmd)
            return _control

        if schema.name == "weak_play_long_string":
            # missing song_title → ask follow-up (Alexa CanFulfillIntent pattern)
            async def _ask():
                await self.ctrl._ask_music_followup(ctx.speaker, ctx.query, ["song_title"])
            return _ask

        # strong_play / weak_play_with_marker → direct play
        async def _play():
            await self.ctrl._safe_music_command(ctx.speaker, ctx.query, "play")
        return _play
