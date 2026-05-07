# marvin-voice-core

[![CI](https://github.com/butthead0819-beep/marvin-voice-core/actions/workflows/ci.yml/badge.svg)](https://github.com/butthead0819-beep/marvin-voice-core/actions/workflows/ci.yml)

A voice-first community AI for Discord — speaks in your voice channel, remembers your members, and has a personality that's actually yours.

> **macOS only.** The voice pipeline uses macOS native audio and a Swift STT script. A Linux/Windows path (Whisper-only fallback) is not yet available — see [Open Questions](#open-questions).

---

## Why this is different

Text bots are everywhere. Voice AI that knows your community isn't.

- **Speaks in Discord voice channels** — not text chat. Reacts to what people say, out loud, in real time.
- **Remembers your community** — stores per-member impressions, relationship stages, highlights, and behavioral patterns in `suki_memory.json`. Marvin knows who you are and acts like it.
- **Ships with a real personality** — default is a nihilistic AI companion (Hitchhiker's Guide–style). Swap it out or tune it to match your community's vibe.

No other self-hosted project combines all three: **voice + personality + community memory**.

---

## What you need

- **macOS** (Monterey 12+ recommended)
- **Python 3.12+**
- **Xcode Command Line Tools** (for the Swift STT script)
  ```bash
  xcode-select --install
  ```
- **API keys** — all required for full functionality:

  | Key | Used for | Where to get it |
  |-----|----------|-----------------|
  | `DISCORD_BOT_TOKEN` | Bot identity | [Discord Developer Portal](https://discord.com/developers/applications) |
  | `GOOGLE_API_KEY` / `GEMINI_API_KEY` | Primary LLM | [Google AI Studio](https://aistudio.google.com/) |
  | `GROQ_API_KEY` | STT cleaner + fallback LLM | [console.groq.com](https://console.groq.com/) |

  TTS uses `edge-tts` (Microsoft Edge TTS) — no API key, bundled in `requirements.txt`.

---

## 5-minute quickstart

```bash
# 1. Clone
git clone https://github.com/butthead0819-beep/marvin-voice-core.git
cd marvin-voice-core

# 2. Install
pip install -r requirements.txt

# 3. Configure API keys
cp .env.example .env
# Edit .env — fill in DISCORD_BOT_TOKEN, GOOGLE_API_KEY, GROQ_API_KEY at minimum

# 4. Set up community memory
# Copy the starter JSON and add your server members
cp docs/memory_schema_starter.json suki_memory.json
# Edit suki_memory.json — rename "example-username" to your Discord usernames

# 5. Run
python main_discord.py
```

In Discord: join a voice channel, then type `/summon` in any text channel.

---

## Community Memory

`suki_memory.json` is the heart of the project. It stores what Marvin knows about each member of your community — not chat logs, but structured observations:

```json
{
  "players": {
    "your-discord-username": {
      "suki_impression": "Marvin's inner monologue about this person",
      "relationship_stage": "陌生人",
      "likes": [],
      "dislikes": [],
      "taboos": [],
      "bias_score": 0.0
    }
  }
}
```

See [`docs/memory_schema_template.md`](docs/memory_schema_template.md) for the full schema with all fields documented.

**Important:** `suki_memory.json` contains personal data. It is gitignored by default — never commit it.

---

## Architecture

The voice pipeline is in `marvin_voice_core/` — decoupled from bot logic so it can be used standalone:

```
marvin_voice_core/
  pipeline.py            — ConversationBuffer, MarvinVoicePipeline
  sink.py                — RealtimeVADSink (Discord audio → PCM, VAD gating)
  stt_handler.py         — Swift STT (primary) + Faster-Whisper (fallback)
  audio_utils.py         — RMS calculation, gain, WAV export
  voice_meta_analyzer.py — per-utterance metadata (speaker, timing, energy)
  atmosphere_tracker.py  — real-time topic/mood tracking from the STT stream
  marmo_server.py        — async webhook relay for external voice jobs
```

`AtmosphereTracker` reads the STT stream and produces a snapshot (gaming / work / food / etc.) that gets injected into the LLM system prompt — so Marvin knows what the room is actually talking about.

`MarmoServer` (port 8765) lets external agents push text into Marvin's voice queue without a direct Python import. Useful for piping in results from shell scripts or other bots.

---

## Personality

The default personality is Marvin from *The Hitchhiker's Guide to the Galaxy* — depressed, existential, and deeply unimpressed. He has a planet-sized brain and is stuck answering questions about your gaming session.

To change the personality: edit `personality_config.py` and the system prompt in `marvin_prompts.py`.

The DNA system (`suki_memory.json → bias_score`, `relationship_stage`) automatically adjusts how Marvin speaks to each person — more warmth for old friends, more formality for strangers. This calibration builds over time from real interactions.

---

## Open Questions

1. **Linux/Windows**: The Swift STT layer (`macos_stt.swift`) is macOS-only. A Whisper-only fallback path exists in `stt_handler.py` but has not been tested on Linux. Contributions welcome.
2. **Docker**: macOS native audio cannot be containerized. Not currently planned.

---

## Contributing

Code comments are in Traditional Chinese (zh-TW) — this project started as a personal bot for a Taiwanese gaming group. English PRs are welcome; translating comments is appreciated but not required.

If you successfully run this on a fresh machine, please open a GitHub Discussions post in the "Show your setup" thread. That single confirmation is the most useful signal this project can receive right now.

---

## License

MIT
