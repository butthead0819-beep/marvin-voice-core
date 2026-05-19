"""SpeakerProfileBuilder — assemble SpeakerProfile from heterogeneous stores.

Pure-read module: tolerates missing stores (each = None) and tolerates per-store
exceptions (logged, treated as missing data). Used by SemanticResolver to fetch
context for vector intent CURATION / DIRECTIONAL slot fills.

Data sources (all optional, dependency-injected):
  - suki:         MemoryManager — birth_year (from suki_impression), age derived
  - music:        MusicMemory   — recent_played from get_top_songs_for_user
  - temperature:  DiscordTemperatureMonitor — current_mood via level mapping
  - channel_members_provider: Callable[[], list[str]] — voice channel state
  - clock:        Callable[[], float] — time_of_day derivation (default time.time)

Design rules:
  - No I/O writes anywhere
  - Each store failure isolated — one broken doesn't break others
  - Missing data → corresponding SpeakerProfile field stays at default
"""
from __future__ import annotations

import datetime
import logging
import re
from typing import Any, Callable, Optional

from intent_agents.semantic_resolver import SpeakerProfile

logger = logging.getLogger(__name__)


_BIRTH_YEAR_RE = re.compile(r"(?:b\.\s*|出生.{0,3}|生於.{0,3}|^|\D)(19\d{2}|20[0-2]\d)\s*年?")

_LEVEL_TO_MOOD = {
    "cold": "reflective",
    "warm": "relaxed",
    "hot":  "energetic",
}


def _extract_birth_year(impression: str) -> Optional[int]:
    """Pull a 4-digit year (1900-2029) from free-text impression. None if absent."""
    if not impression:
        return None
    m = _BIRTH_YEAR_RE.search(impression)
    if not m:
        return None
    try:
        year = int(m.group(1))
    except (TypeError, ValueError):
        return None
    if 1900 <= year <= 2029:
        return year
    return None


def _time_of_day(hour: int) -> str:
    """Local-hour → bucket. Aligned with MusicMemory.time_slot but English keys."""
    if hour < 5 or hour >= 23:
        return "late_night"
    if hour < 12:
        return "morning"
    if hour < 18:
        return "afternoon"
    return "evening"


class SpeakerProfileBuilder:
    """Compose SpeakerProfile per call (cheap; not cached — stores mutate)."""

    def __init__(
        self,
        suki: Optional[Any] = None,
        music: Optional[Any] = None,
        temperature: Optional[Any] = None,
        channel_members_provider: Optional[Callable[[], list[str]]] = None,
        clock: Optional[Callable[[], float]] = None,
    ):
        self.suki = suki
        self.music = music
        self.temperature = temperature
        self.channel_members_provider = channel_members_provider
        self.clock = clock  # None → time_of_day stays None (don't fake-derive)

    def build(self, speaker: str) -> SpeakerProfile:
        return SpeakerProfile(
            speaker=speaker,
            birth_year=self._birth_year(speaker),
            age=self._age(speaker),
            recent_played=self._recent_played(speaker),
            time_of_day=self._time_of_day(),
            current_mood=self._mood(),
            who_else_in_channel=self._who_else(speaker),
        )

    # ── Per-field extractors (isolated try/except, never raise to caller) ──

    def _birth_year(self, speaker: str) -> Optional[int]:
        if self.suki is None:
            return None
        try:
            if not self.suki.has_player(speaker):
                return None
            data = self.suki.get_player_memory(speaker) or {}
            return _extract_birth_year(data.get("suki_impression", ""))
        except Exception as e:
            logger.warning(f"⚠️ [ProfileBuilder] suki birth_year 失敗：{e}")
            return None

    def _age(self, speaker: str) -> Optional[int]:
        yr = self._birth_year(speaker)
        if yr is None or self.clock is None:
            return None
        try:
            now_year = datetime.datetime.fromtimestamp(self.clock()).year
            return now_year - yr
        except Exception:
            return None

    def _recent_played(self, speaker: str) -> list[str]:
        if self.music is None:
            return []
        try:
            songs = self.music.get_top_songs_for_user(speaker, limit=10) or []
        except Exception as e:
            logger.warning(f"⚠️ [ProfileBuilder] music recent_played 失敗：{e}")
            return []
        out: list[str] = []
        for s in songs:
            title = s.get("title", "") if isinstance(s, dict) else ""
            if title:
                out.append(title)
        return out

    def _time_of_day(self) -> Optional[str]:
        if self.clock is None:
            return None
        try:
            hour = datetime.datetime.fromtimestamp(self.clock()).hour
            return _time_of_day(hour)
        except Exception:
            return None

    def _mood(self) -> Optional[str]:
        if self.temperature is None:
            return None
        try:
            level = getattr(self.temperature, "level", None)
            if isinstance(level, str):
                return _LEVEL_TO_MOOD.get(level)
            return None
        except Exception as e:
            logger.warning(f"⚠️ [ProfileBuilder] temperature mood 失敗：{e}")
            return None

    def _who_else(self, speaker: str) -> list[str]:
        if self.channel_members_provider is None:
            return []
        try:
            members = self.channel_members_provider() or []
        except Exception as e:
            logger.warning(f"⚠️ [ProfileBuilder] channel_members 失敗：{e}")
            return []
        return [m for m in members if m != speaker]
