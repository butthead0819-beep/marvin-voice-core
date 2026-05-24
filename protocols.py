"""
Marvin Bot — Service Protocols (Phase 2 abstraction layer)

Each Protocol defines the minimal interface that a component must satisfy.
Current implementations are in-process; Phase 3 will swap to cloud services
that satisfy the same interface without changing callers.

Dependency graph:
  STTService   ← marvin_voice_core/stt_handler.py  → Phase 3: Deepgram
  LLMClient    ← gemini_router.py                   → Phase 3: OpenRouter / LiteLLM
  MemoryStore  ← suki_memory.py (SQLite)            → Phase 3: PostgreSQL
"""

from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable


# ── Speech-to-Text ────────────────────────────────────────────────────────────

@runtime_checkable
class STTService(Protocol):
    """Transcribe a WAV file to text.

    Returns:
        (transcribed_text, engine_name, meta)
            e.g. ("馬文你好", "Swift", {"avg_confidence": 0.87, "min_confidence": 0.42,
                                         "avg_pause_duration": 0.15, "speaking_rate": 145.3})

    meta is engine-specific and may be empty ({}). For Swift on macOS 13+, it includes
    confidence + prosody features used by J1 calibration and VAD temperature heuristics.
    """

    async def transcribe(
        self,
        wav_path: str,
        *,
        speaker: str = "",
        context: str = "",
    ) -> tuple[str, str, dict]: ...


# ── Large-Language-Model client ───────────────────────────────────────────────

@runtime_checkable
class LLMClient(Protocol):
    """Minimal LLM interface — one blocking call and one streaming call."""

    async def complete(
        self,
        system: str,
        user: str,
        *,
        is_json: bool = False,
        temperature: float | None = None,
    ) -> str: ...

    async def stream_text(
        self,
        system: str,
        user: str,
        *,
        temperature: float | None = None,
    ) -> AsyncIterator[str]: ...


# ── Memory / player state store ───────────────────────────────────────────────

@runtime_checkable
class MemoryStore(Protocol):
    """Read/write player profiles and per-player state."""

    def get_player_memory(self, username: str) -> dict: ...
    def increment_stat(self, username: str, field: str, delta: float = 1.0) -> None: ...
    def enqueue_news(self, username: str, news_text: str) -> None: ...
    def pop_news(self, username: str) -> str | None: ...
    def get_rich_context(self, username: str) -> str: ...
    def adjust_bias(self, username: str, delta: float) -> None: ...
    def update_player_memory(self, username: str, new_info: dict) -> None: ...
    def flush(self) -> None: ...
