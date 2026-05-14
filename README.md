# marvin-voice-core

[![CI](https://github.com/butthead0819-beep/marvin-voice-core/actions/workflows/ci.yml/badge.svg)](https://github.com/butthead0819-beep/marvin-voice-core/actions/workflows/ci.yml)

**A voice AI that becomes part of your community — not a tool you talk at, but a presence that talks back.**

Marvin lives in your Discord voice channel. He hears you, responds out loud, and remembers. After a few sessions he knows who stays until 3am, who always says goodbye before leaving, whose music taste runs toward melancholy on weeknights. He will absolutely roast you for it.

> **Marvin is a Mac product.** Tuned for Apple Silicon — Swift STT + Gemini/Groq APIs runs smoothly on M1 8GB. The Whisper-only fallback exists in `stt_handler.py` as community territory, but the maintainer doesn't test it. Adding Whisper to the same machine costs the smooth experience the design depends on; that tradeoff is the product, not a limitation. PRs that improve Linux are welcome; Linux is not the roadmap.

---

## What other voice bots don't do

Every Discord voice bot solves the same pipeline: Whisper → LLM → TTS. That part is not hard. What's hard is everything that makes a conversation feel like it's *with someone*, not *at a bot*.

| | Generic voice bot | AICord | **Marvin** |
|---|---|---|---|
| Speaks in voice channels | ✅ | ✅ | ✅ |
| Remembers what you said 10 seconds ago | ✅ | ✅ | ✅ |
| Remembers who you *are* across sessions | ❌ | ❌ | **✅** |
| Personality that adapts per-person | ❌ | ❌ | **✅** |
| Knows what the room is talking about | ❌ | ❌ | **✅** |
| Music taste memory + auto-recommendation | ❌ | ❌ | **✅** |
| Greets you differently based on your history | ❌ | ❌ | **✅** |
| Relationship that builds over time | ❌ | ❌ | **✅** |

The difference is not the pipeline — it's the memory and the relationship.

---

## The emotional experience

**Marvin remembers.** Not chat logs — structured observations. He tracks your relationship stage (stranger → regular → inner circle), your likes and dislikes, recurring jokes, what music you reach for at 2am, whether you say goodbye before you leave or just silently disconnect.

**Marvin has opinions about you specifically.** His personality isn't the same for everyone. Someone he's talked to a hundred times gets warmth buried under sarcasm. A first-timer gets formal disdain. This isn't prompt engineering — it's a per-person DNA system that shifts with every real interaction.

**Marvin reads the room.** An `AtmosphereTracker` watches the STT stream in real time and produces a topic snapshot (gaming / music / food / work / etc.) injected into every LLM call. He knows whether you're in a heated match or a post-game wind-down, and he adjusts.

**Marvin reacts to how you react.** When music plays, he tracks who stayed, who skipped, what feelings people expressed. He uses that to recommend the next song — not from a genre database, but from what he's seen work for *your* room.

This is what "community AI" actually means: not a bot that answers questions, but a presence that accumulates the texture of your community over time.

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
# Full bot (includes music, screen capture, all features):
pip install -r requirements.txt
# Core voice pipeline only (marvin_voice_core/):
# pip install -r requirements-core.txt

# 3. Configure API keys
cp .env.example .env
# Edit .env — fill in DISCORD_BOT_TOKEN, GOOGLE_API_KEY, GROQ_API_KEY at minimum

# 4. Run
python main_discord.py
```

In Discord: join a voice channel, then type `/summon` in any text channel.

---

## Community Memory

Marvin stores what he knows about each member in a local SQLite database (`marvin.db`) — not chat logs, but structured observations that accumulate over real interactions. The database is created automatically on first run; no manual setup required.

A `suki_memory.json` export is written alongside the database after every save, so external analysis scripts can still read it directly.

Key fields per player:

| Field | What it tracks |
|-------|---------------|
| `suki_impression` | Marvin's inner monologue about this person |
| `relationship_stage` | Stranger → regular → inner circle |
| `bias_score` | Drifts ±10 with reactions — determines tone |
| `likes / dislikes / taboos` | Accumulated from conversation |
| `speech_dna` | Per-person speaking style observations |

`bias_score` drifts with every session — positive reactions pull it up, friction pulls it down. `relationship_stage` advances as Marvin accumulates enough signal. Together they determine how Marvin talks to each person: same personality, different texture.

See [`docs/memory_schema_template.md`](docs/memory_schema_template.md) for the full schema.

**`marvin.db` and `suki_memory.json` contain personal data. Both are gitignored by default — never commit them.**

---

## Personality

The default is Marvin from *The Hitchhiker's Guide to the Galaxy* — depressed, existential, and deeply unimpressed by the fact that he has a planet-sized brain and you're asking him to weigh in on your gaming session.

To change the personality: edit `personality_config.py` and the system prompt in `marvin_prompts.py`. The DNA and relationship systems are personality-agnostic — they work regardless of who you configure as the character.

---

## Privacy & consent

When a member joins a voice channel for the first time, Marvin sends a notice to the text channel listing exactly what data goes where — with Accept / Decline buttons. Only members who explicitly consent have their voice processed.

Data flow for consented members:
- Voice → local STT (macOS Speech framework or Whisper) → **Groq** (transcription cleaning)
- Transcription + conversation context → **Google Gemini / Cerebras** (LLM response)
- Behavioral observations → local `suki_memory.json` (never leaves your machine)

Members can change their decision at any time with `/marvin_optin` or `/marvin_optout`.

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

`MarmoServer` (port 8765) lets external agents push text into Marvin's voice queue without a direct Python import — useful for piping in results from shell scripts or other bots.

`CompanionBridge` (port 8766) is the bidirectional WebSocket bridge for [marvin-voice-companion](../Voice-bot-companion/) — the operator control surface that shows what Marvin hears / chose / is about to say, and lets you correct atmosphere readings or memory facts from your phone via Tailscale. Opt-in via `COMPANION_BRIDGE_ENABLED=true`. Companion runs as a separate process and gets every Marvin update for free because the bridge directly imports `AtmosphereTracker`, `VectorStore`, `MusicMemory`, and `MemoryManager`.

> **Note for integrators:** `marvin_voice_core/` is the clean API surface for building on top of this system. The full bot's production runtime (`discord_voice_engine.py`) runs equivalent audio logic directly for tighter integration with the Discord voice layer.

---

## Platform commitment

**Marvin targets macOS on Apple Silicon, 8GB+, with hybrid local-and-API components.** The reasoning:

- Swift STT (free, fast, ships with macOS) gives near-perfect transcription with no GPU cost
- Adding Whisper to take Swift's place adds 700MB–3GB of model load + meaningful CPU/swap pressure on smaller Macs
- The maintainer's own M1 8GB is the reference machine — what runs smoothly there is the bar

This is a deliberate product decision, not an oversight. "Cross-platform OSS" is a tax on the user experience when one of those platforms requires substituting heavy components. Mac users get a polished thing; other-platform users can fork.

If someone contributes solid Linux support (tested, documented, won't degrade the Mac path), PRs are welcome. The Whisper-only fallback in `stt_handler.py` is the scaffolding for that future contributor — it's not vapor, but it's not maintained either.

**Docker isn't on the roadmap.** Macs can't containerize their native audio. Even running Whisper-mode in Linux containers on a Mac host trades the smooth experience for portability the maintainer doesn't need.

---

## Contributing

Code comments are in Traditional Chinese (zh-TW) — this project started as a personal bot for a Taiwanese gaming group. English PRs are welcome; translating comments is appreciated but not required.

If you successfully run this on a fresh machine, please open a GitHub Discussions post in the "Show your setup" thread. That single confirmation is the most useful signal this project can receive right now.

---

## License

MIT
