"""MusicAgent v2 — declarative version (validation target for new arch).

Behavior parity goal vs v1：
  - 同 query → 同 Bid(name, confidence, missing_slots)
  - reason 格式可微調，但要可解析
  - dense bid 0.0 + reason 是 v2 新增（v1 是 None），驗證 negative space 表達

Schema priority order（declare 順序 = first-match-wins，與 confidence 解耦）：
  1. control_skip / control_pause / control_resume / control_stop (0.95)
  2. strong_play (0.95)
  3. weak_play_directional (0.50, missing=["directional_resolution"])  ← 抽象修飾先攔
  4. weak_play_specific (0.95, no missing)                            ← artist的song≥2字
  5. weak_play_with_marker (0.80)
  6. weak_play_artist_only (0.85, missing=["song_choice"])            ← CURATION
  7. weak_play_long_string (0.55, missing=["song_title"])

3/4/6 是 5/21 vector intent 三檔分流新增；對 v1（MusicAgent）刻意 diverge，
parity validator Gate 1 預期 fail（見 memory/project_vector_intent_5_21.md）。
"""
from __future__ import annotations

import re
from collections import Counter

from intent_agents.base import DeclarativeIntentAgent, IntentSchema
from intent_agents.constants import (
    MUSIC_PAUSE_KW,
    MUSIC_RESUME_KW,
    MUSIC_SKIP_KW,
    MUSIC_STOP_KW,
    STRONG_PLAY_KW,
    WEAK_PLAY_KW,
)
from intent_bus import IntentContext


# Music intent markers — same as v1
_MUSIC_INTENT_MARKERS = ("的", "歌", "曲", "音樂", "mv", "ost", "歌詞", "歌手",
                         "一首", "那首", "這首")

# Directional modifiers — 抽象修飾（符合年紀 / 適合心情 / 像 X 那種），
# 需 semantic resolver 解成具體年代/情緒。必須在 specific/marker/artist 之前攔下。
_DIRECTIONAL_MODIFIERS = ("符合.*?的", "適合.*?的", "像.*?那種")

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

        skip_kws = _kw_alt(MUSIC_SKIP_KW)
        pause_kws = _kw_alt(MUSIC_PAUSE_KW)
        resume_kws = _kw_alt(MUSIC_RESUME_KW)
        stop_kws = _kw_alt(MUSIC_STOP_KW)
        strong_kws = _kw_alt(STRONG_PLAY_KW)
        weak_kws = _kw_alt(WEAK_PLAY_KW)
        markers = _kw_alt(_MUSIC_INTENT_MARKERS)
        directional = "|".join(_DIRECTIONAL_MODIFIERS)

        # Vector intent 三檔分流（priority 與 confidence 刻意解耦，靠 declare 順序）：
        #   directional(0.50) 先攔 → specific(0.95) → with_marker(0.80) → artist_only(0.85) → long_string(0.55)
        # directional 必須最先：「播放周杰倫符合我年紀的歌」同時含「的」與 artist，但要判 directional。
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
            # DIRECTIONAL — 抽象修飾，需 resolver 解年代/情緒。0.50，缺 directional_resolution。
            IntentSchema("weak_play_directional", 0.50,
                         patterns=[f"(?P<kw>{weak_kws})(?=.*(?:{directional}))"],
                         required_slots=["directional_resolution"],
                         reason_template="weak_play_directional:{kw}"),
            # SPECIFIC — artist「的」song（後段 ≥2 字）= 完整曲目，0.95，無 missing。
            # ⚠️ SPECIFIC_CONF=0.95 採驗收表；若維持舊 with_marker 0.80，把這行 0.95 改 0.80。
            IntentSchema("weak_play_specific", 0.95,
                         patterns=[f"(?P<kw>{weak_kws}).*的(?P<song>\\S{{2,}})"],
                         reason_template="weak_play_specific:{kw}->{song}"),
            # Weak play + marker（marker 在 query 任意處出現，但後段不足成 specific）
            IntentSchema("weak_play_with_marker", 0.80,
                         patterns=[f"(?P<kw>{weak_kws})(?=.*(?:{markers}))"],
                         reason_template="weak_play+marker:{kw}"),
            # CURATION — 純 artist token（≤4 字，緊接 kw 且收尾），把選擇權交給 Marvin。
            # 0.85（高，仍 winning），缺 song_choice → bus 路由到 resolver 補完。
            IntentSchema("weak_play_artist_only", 0.85,
                         patterns=[
                             f"(?P<kw>{weak_kws})[，,、！!？?。. ]*(?P<target>\\S{{2,4}})$"
                         ],
                         required_slots=["song_choice"],
                         reason_template="weak_play_curation:{kw}->{target}"),
            # Weak play + 後續長字串（artist 名超過 4 字 / 雜訊），0.55 兜底追問。
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
        # artist_only / long_string 都吃 kw 後的 target → 同樣過 UI 黑名單
        if schema.name not in ("weak_play_long_string", "weak_play_artist_only"):
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
