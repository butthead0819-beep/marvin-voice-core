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

from typing import Any, AsyncIterator, Protocol, runtime_checkable


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


# ── Audio input source ────────────────────────────────────────────────────────

@runtime_checkable
class AudioSource(Protocol):
    """Transport-agnostic audio input abstraction.

    Both Discord's RealtimeVADSink and the local-mic LocalMicSink satisfy this
    interface.  The contractual obligation: when the end of a speech segment is
    detected, fire::

        await on_speech_cut_callback(user_id, pcm_bytes, timestamp,
                                     *, is_wake_check=False)

    — the same signature the existing voice pipeline already accepts.
    Callers that wire an AudioSource into the pipeline are decoupled from
    whether audio originates from Discord, a local microphone, or a test
    fixture.
    """

    on_speech_cut_callback: Any  # Callable: (user_id, pcm, timestamp, *, is_wake_check=False)

    async def start(self) -> None: ...


# ── Audio output / playback device ───────────────────────────────────────────

@runtime_checkable
class PlaybackDevice(Protocol):
    """Transport-agnostic audio output abstraction.

    Both DiscordPlaybackDevice (thin wrapper around a discord VoiceClient) and
    a future LocalSpeakerDevice satisfy this interface.  Callers that need to
    play audio are decoupled from whether output goes to Discord or a local
    speaker.

    Note: ``stop()`` maps to ``voice_client.stop_playing()`` in the Discord
    adapter (a project-local method), **not** ``voice_client.stop()``.
    """

    def play(self, source: Any, *, after=None) -> None: ...
    def is_playing(self) -> bool: ...
    def stop(self) -> None: ...
    def is_connected(self) -> bool: ...
    def arm_mixer(self, source: Any) -> None:
        """啟動持續性 mixer 播放。Discord=vc.play(opus+bitrate)、Local=起本機泵。"""
        ...
