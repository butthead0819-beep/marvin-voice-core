# Architecture

The voice pipeline lives in `marvin_voice_core/`, decoupled from bot logic so it can be used standalone:

```
marvin_voice_core/
  pipeline.py            — ConversationBuffer, MarvinVoicePipeline
  sink.py                — RealtimeVADSink (Discord audio → PCM, VAD gating)
  stt_handler.py         — Swift STT (primary) + Faster-Whisper (fallback)
                           Returns (text, engine_name, meta); meta carries Swift prosody features
  audio_utils.py         — RMS, gain, WAV export
  voice_meta_analyzer.py — per-utterance metadata (speaker, timing, energy)
  atmosphere_tracker.py  — real-time topic/mood tracking from the STT stream
  marmo_server.py        — async webhook relay for external voice jobs
```

## IntentBus dispatch

Wake-word triggered intents go through a separate dispatch system. On wake, all agents bid in parallel and the max-confidence handler wins:

```
intent_bus.py     — IntentBus + Bid + IntentContext
intent_agents/    — IntentAgent implementations, each declaring mode_compatible
                    (normal / stream / game): music, playback control, find-song,
                    nemoclaw routing, hallucination guard, + game-mode agents
intent_judges/    — Parallel STT judges race: regex (J1) / Groq Llama 8B (J2) /
                    cleaner (J3), FIRST_COMPLETED coordinator with max-confidence
                    fallback; writes records/judge_outcomes.jsonl for offline analysis
```

To add a new intent, write an `IntentAgent` subclass and register it with `VoiceController._intent_bus` — never touch the `voice_controller` if/elif chain. See [CLAUDE.md](../CLAUDE.md) for the bid contract (sync ≤5ms, dense 0.0 reasons, mode gating).

## External bridges

`MarmoServer` (port 8765) lets external agents push text into Marvin's voice queue without a Python import.

`CompanionBridge` (port 8766, opt-in via `COMPANION_BRIDGE_ENABLED=true`) is the bidirectional WebSocket bridge for the operator control surface — it shows what Marvin hears / chose / is about to say, and lets you correct atmosphere readings or memory facts from your phone via Tailscale.

## For integrators

`marvin_voice_core/` is the clean API surface for building on top of this system. The full bot's production runtime (`discord_voice_engine.py`) runs equivalent audio logic directly for tighter integration with the Discord voice layer.
