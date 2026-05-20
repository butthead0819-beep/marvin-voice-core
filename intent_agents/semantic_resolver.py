"""Semantic resolver — fills CURATION / DIRECTIONAL slot 的 vector intent dimension。

Called when a winning Bid has missing_slots ∈ {"song_choice", "directional_resolution"}.
Reads speaker profile (age / recent_played / time_of_day / mood) and rewrites the
raw query into a specific yt-dlp-friendly string, then returns ResolvedIntent
so the caller can re-dispatch with depth+1.

This module has **no prod wiring** as of 2026-05-20 — it's exercised only by
tests/test_music_curation_intent.py. Once v2 schemas + bus dispatch are updated
to emit / route missing_slots, this resolver becomes the curation backbone.

Design notes:
- ≥1 LLM call per CURATION case → Cerebras 8b (~150ms target), not Groq
- depth ≥ MAX_REWRITE_DEPTH → return None (caller falls through to Marvin LLM)
- Unknown missing_slot name → return None (don't fake-handle)
- Cerebras failure / no client / garbage JSON → return None (graceful degrade)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

MAX_REWRITE_DEPTH = 2  # depth >= this → no further resolve, caller兜底

# Known slots that this resolver understands. Anything else → None.
_KNOWN_SLOTS = frozenset({"song_choice", "directional_resolution"})


@dataclass(frozen=True)
class SpeakerProfile:
    """Per-speaker context fed into the resolver prompt.

    All fields optional — resolver tolerates missing data (prompt simply omits
    that dimension). For 5/21 vertical slice the data sources are mocked;
    later this dataclass will be built from suki_memory + music store +
    atmosphere_tracker + voice channel state.
    """
    speaker: str
    age: Optional[int] = None
    birth_year: Optional[int] = None
    recent_played: list[str] = field(default_factory=list)
    time_of_day: Optional[str] = None      # "morning" / "afternoon" / "evening" / "late_night"
    current_mood: Optional[str] = None     # from atmosphere_tracker
    who_else_in_channel: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ResolvedIntent:
    """Output of resolver — feeds back into bus for re-dispatch."""
    rewritten_query: str   # yt-dlp friendly, e.g. "周杰倫 夜曲"
    quip: str = ""         # ≤20 chars Marvin-style announcer line (optional)
    depth: int = 1         # rewrite chain depth; caller passes this to next dispatch


# ── Prompt building ────────────────────────────────────────────────────────

_SYS_PROMPT = (
    "你是 Marvin（厭世幽默風）的音樂選曲助理。"
    "user 給一個未指定具體曲目的點歌請求（CURATION）或帶抽象修飾的請求（DIRECTIONAL），"
    "請從 user 的 profile（年齡 / 最近聽過 / 時段 / 心情）推斷一首具體歌曲。\n\n"
    "硬規則：\n"
    "1. 必須回 JSON：{\"song\": str, \"year\": int|null, \"quip\": str}\n"
    "2. song 必須是真實存在的歌曲名稱，不要編造\n"
    "3. 不要選 recent_played 內已有的歌\n"
    "4. quip ≤20 字，Marvin 厭世幽默語氣（可選；不會就留空）\n"
    "5. directional_resolution：把抽象修飾（符合年紀 / 心情 / 像 X 那種）轉換成具體年代或情緒對應的歌\n"
)


def _build_user_message(slot: str, raw_query: str, profile: SpeakerProfile) -> str:
    """Compose user-side prompt block. Keep tokens tight—this is on hot path."""
    lines: list[str] = []
    lines.append(f"speaker: {profile.speaker}")
    if profile.age is not None:
        lines.append(f"age: {profile.age}")
    if profile.birth_year is not None:
        lines.append(f"birth_year: {profile.birth_year}")
    if profile.time_of_day:
        lines.append(f"time_of_day: {profile.time_of_day}")
    if profile.current_mood:
        lines.append(f"current_mood: {profile.current_mood}")
    if profile.recent_played:
        lines.append("recent_played: " + ", ".join(profile.recent_played[:10]))
    if profile.who_else_in_channel:
        lines.append("who_else_in_channel: " + ", ".join(profile.who_else_in_channel))
    lines.append(f"missing_slot: {slot}")
    lines.append(f"raw_query: {raw_query}")
    return "\n".join(lines)


# ── Parsing ────────────────────────────────────────────────────────────────

def _parse_response(content: str, raw_query: str, depth: int) -> Optional[ResolvedIntent]:
    """Parse Cerebras JSON output into ResolvedIntent. Return None on any failure."""
    stripped = (content or "").strip()
    if not stripped.startswith("{"):
        return None
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
    song = str(data.get("song", "")).strip()[:100]  # hard cap: 防 LLM 幻覺長 string 污染 yt-dlp 查詢
    if not song:
        return None
    quip = str(data.get("quip", "")).strip()[:40]  # hard cap defensive
    rewritten = _compose_query(raw_query, song)
    return ResolvedIntent(rewritten_query=rewritten, quip=quip, depth=depth + 1)


def _compose_query(raw_query: str, song: str) -> str:
    """Build yt-dlp-friendly query.

    If raw_query already contains the song (rare), keep raw_query.
    Otherwise prefix raw_query (the artist hint) before song.
    Strip trailing directional modifiers like '符合我年紀的歌' since the
    song selection has already incorporated that intent.
    """
    if song in raw_query:
        return raw_query
    # Strip common directional suffixes — song already encodes the directional resolve
    cleaned = raw_query
    for suffix in ("符合我年紀的歌", "符合我年紀的", "符合年紀的", "適合我的",
                   "像他那種的歌", "那種的", "心情的歌"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    cleaned = cleaned.strip()
    return f"{cleaned} {song}".strip() if cleaned else song


# ── Resolver ───────────────────────────────────────────────────────────────

class SemanticResolver:
    """Fills missing dimensions in a vector intent via Cerebras 8b LLM call."""

    def __init__(self, cerebras_client: Any, model: str = "llama-3.1-8b"):
        self.client = cerebras_client
        self.model = model

    async def resolve(
        self,
        missing_slot: str,
        raw_query: str,
        profile: SpeakerProfile,
        depth: int = 0,
    ) -> Optional[ResolvedIntent]:
        # Anti-infinite-loop: bus must re-dispatch with depth+1; we refuse at MAX.
        if depth >= MAX_REWRITE_DEPTH:
            return None

        if missing_slot not in _KNOWN_SLOTS:
            return None

        if self.client is None:
            return None

        user_msg = _build_user_message(missing_slot, raw_query, profile)
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": _SYS_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.3,
                max_tokens=200,
                response_format={"type": "json_object"},
            )
        except Exception as e:
            logger.warning(f"⚠️ [SemanticResolver] Cerebras 失敗，回 None 兜底: {e}")
            return None

        content = response.choices[0].message.content
        return _parse_response(content, raw_query, depth)
