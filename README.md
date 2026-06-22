# marvin-voice-core

[![CI](https://github.com/butthead0819-beep/marvin-voice-core/actions/workflows/ci.yml/badge.svg)](https://github.com/butthead0819-beep/marvin-voice-core/actions/workflows/ci.yml)

**A Discord bot that joins your voice channel, hears you talk, and talks back out loud — and remembers you.**

Marvin lives in your Discord voice channel. After a few sessions he knows who stays until 3am, who always says goodbye before leaving, whose music taste runs toward melancholy on weeknights. He will absolutely roast you for it.

📖 **The story** — how this grew from a toy into a platform, Simon → Suki → Marvin, in three months: **[read the illustrated history](https://butthead0819-beep.github.io/marvin-voice-core/marvin-story.html)** (中 / EN).

> **Marvin is a macOS / Apple Silicon product.** Tuned for Swift STT + Gemini/Groq on M1 8GB. The Whisper-only fallback in `stt_handler.py` is community territory, not maintained. See [docs/PHILOSOPHY.md](docs/PHILOSOPHY.md) for the why, the tested footprint, and the Linux/Docker stance.

---

## What other voice bots don't do

Every Discord voice bot solves the same pipeline: STT → LLM → TTS. That part is not hard. What's hard is everything that makes a conversation feel like it's *with someone*, not *at a bot*.

| | Generic voice bot | **Marvin** |
|---|---|---|
| Speaks in voice channels | ✅ | ✅ |
| Remembers what you said 10 seconds ago | ✅ | ✅ |
| Remembers who you *are* across sessions | ❌ | **✅** |
| Personality that adapts per-person | ❌ | **✅** |
| Knows what the room is talking about | ❌ | **✅** |
| Music taste memory + auto-recommendation | ❌ | **✅** |
| Draws your funniest moments into a comic | ❌ | **✅** |
| Relationship that builds over time | ❌ | **✅** |

The difference is not the pipeline — it's the memory and the relationship.

- **Marvin remembers** — not chat logs, but structured observations: your relationship stage (stranger → regular → inner circle), likes/dislikes, recurring jokes, what music you reach for at 2am.
- **Marvin has opinions about you specifically** — a per-person DNA system, not one prompt for everyone. A hundred-session regular gets warmth buried under sarcasm; a first-timer gets formal disdain.
- **Marvin reads the room** — an `AtmosphereTracker` watches the STT stream in real time and injects a topic/mood snapshot (gaming / music / food / work) into every LLM call.
- **Marvin reacts to how you react** — when music plays he tracks who stayed, who skipped, what people felt, and uses that to recommend the next song from what works for *your* room.
- **Marvin draws what happened** — when a session winds down he picks out the funniest moments and renders them as a Japanese-style comic page, posted back to your diary channel. No one asks for it; it just shows up.

---

## What you need

- **macOS** (Monterey 12+ recommended), **Python 3.12+**
- **Xcode Command Line Tools** (for the Swift STT script): `xcode-select --install`
- **API keys** — all required for full functionality:

  | Key | Used for | Where to get it |
  |-----|----------|-----------------|
  | `DISCORD_BOT_TOKEN` | Bot identity | [Discord Developer Portal](https://discord.com/developers/applications) |
  | `GOOGLE_API_KEY` / `GEMINI_API_KEY` | Primary LLM | [Google AI Studio](https://aistudio.google.com/) |
  | `GROQ_API_KEY` | STT cleaner + fallback LLM | [console.groq.com](https://console.groq.com/) |

  TTS uses `edge-tts` (Microsoft Edge TTS) — no API key, bundled in `requirements.txt`.

## 5-minute quickstart

```bash
# 1. Clone
git clone https://github.com/butthead0819-beep/marvin-voice-core.git
cd marvin-voice-core

# 2. Install — full bot (music, screen capture, all features):
pip install -r requirements.txt
#    or core voice pipeline only:  pip install -r requirements-core.txt

# 3. Configure API keys
cp .env.example .env   # fill in DISCORD_BOT_TOKEN, GOOGLE_API_KEY, GROQ_API_KEY

# 4. Run
python main_discord.py
```

In Discord: join a voice channel, then type `/summon` in any text channel.

> **Streamer? Want the shortest path?** See [docs/STREAMER_SETUP.md](docs/STREAMER_SETUP.md) — a non-developer 5-minute guide with a one-line installer and DM-the-maintainer fallback.

---

## Games

Three multiplayer voice games in `game/`, each backed by a cog + engine + LLM judge. All are voice-driven (players talk, Marvin narrates outcomes via TTS) and dispatch through the IntentBus with `mode_compatible={"game"}`.

| Game | Cog | What it is |
|---|---|---|
| **Busted** | `cogs/game_cog.py` | Setter picks a secret answer, others race to buzz on LLM-generated clues |
| **Busted99** | `cogs/busted99_cog.py` | 1–99 range-narrowing with counter-intuitive scoring: guessing the answer = 0 points, getting last-2-wrong = 100 |
| **TurtleSoup (海龜湯)** | `cogs/turtle_soup_cog.py` | Paradox riddle; LLM judges yes/no/irrelevant, with a hint graph for personalised ordering |

See `game/busted99/ARCHITECTURE.md` and `game/turtle_soup/ARCHITECTURE.md` for design notes.

## Session comics

When a voice session winds down, Marvin replays the transcript, finds the genuinely funny beats (a punchline, a reversal, someone getting roasted), and lays them out as a one-page Japanese-style comic — paneling, camera distance, and a Hero panel for the biggest laugh — then posts it to your diary channel (`#馬文的厭世日記` / `marvin-diary`). Nobody triggers it; it fires on silence at the end of a session.

It only draws when there's an actual highlight (no laughs → no comic), needs at least a handful of exchanges so short chats don't burn API budget, and de-dupes per session so the same moment isn't drawn twice. Image generation goes through the paid-usage guard with daily/monthly caps. The pipeline lives in `diary_comic/` (see `diary_comic/MANGA_DESIGN.md`).

## Community Memory

Marvin stores what he knows about each member in a local SQLite database (`marvin.db`) — structured observations that accumulate over real interactions, plus recent transcripts for short-term recall. Created automatically on first run. A `suki_memory.json` export is written after every save for external analysis scripts.

| Field | What it tracks |
|-------|---------------|
| `suki_impression` | Marvin's inner monologue about this person |
| `relationship_stage` | Stranger → regular → inner circle |
| `bias_score` | Drifts ±10 with reactions — determines tone |
| `likes / dislikes / taboos` | Accumulated from conversation |
| `speech_dna` | Per-person speaking style observations |

`bias_score` and `relationship_stage` together determine how Marvin talks to each person: same personality, different texture. Full schema in [`docs/memory_schema_template.md`](docs/memory_schema_template.md).

**`marvin.db` and `suki_memory.json` contain personal data — both gitignored by default, never commit them.**

## Personality

The default is Marvin from *The Hitchhiker's Guide to the Galaxy* — depressed, existential, unimpressed that he has a planet-sized brain and you want his take on your gaming session. To change it: edit `personality_config.py` and the system prompt in `marvin_prompts.py`. The DNA and relationship systems are personality-agnostic.

---

## Privacy & consent

When a member first joins a voice channel, Marvin posts a notice listing exactly what data goes where, with Accept / Decline buttons. Only members who explicitly consent have their voice processed. They can change their mind anytime with `/marvin_optin` or `/marvin_optout`.

Data flow for consented members:
- Voice → local STT (macOS Speech framework or Whisper); when the cloud cleaner is enabled, audio goes to **Groq** for transcription cleaning
- Transcription + context → **Google Gemini / Cerebras** (LLM response)
- Session highlights → **Google Gemini** (text + image models) when a session comic is generated
- Behavioral observations → local `suki_memory.json` (never leaves your machine)

Marvin runs on your own machine — there is no central server collecting data across deployments.

Marvin uses **tiered retention**, not blanket zero-data-retention: raw wording ages out, while privacy-safe abstractions (embeddings, behavioral summaries) are kept so the bot can recall and improve. Each tier and its enforcement:

| Tier | Data | Where it lives | Rule | Enforced by |
|------|------|----------------|------|-------------|
| **Seconds** | Raw audio | RAM + per-utterance temp WAV | Deleted in a `finally` block right after transcription — never persisted | `discord_voice_engine.py` audio flush |
| **Hours**¹ | Operational / STT debug logs | `bot_stdout.log`, `stt_history.log`, main bot log | Rotating, **size-capped** (5 MB × 3, 10 MB × 5); oldest chunk auto-discarded | `RotatingFileHandler` in `main_discord.py` |
| **Days** | Raw transcripts | local `marvin.db` | **Deleted** after 14 days; live bot never reads older than 7 days | `scripts/prune_transcripts.py` |
| **Days** | Self-improvement signals (`records/*.jsonl`: judge / gaps / rescue) | local files | Raw wording **replaced with a one-way SHA-1 hash** after 14 days (keeps de-dup / distinct counts working; original text unrecoverable) | `scripts/scrub_improvement_raw.py` |
| **Long-term** | Semantic memory | local vector store | Conversation **embeddings** (no raw text) retained for cross-session recall | — (memory core, not pruned) |
| **Long-term** | Behavioral observations & summaries | local `marvin.db` / `suki_memory.json` | Abstracted community memory retained; no verbatim transcripts | — (memory core, not pruned) |

The two **Days** rules run nightly at **03:00** via the `feedbackbatch` launchd job (`run_feedback_batch.py` → `zdr_scrub` + `transcript_prune`).

¹ The **Hours** tier is bounded by file *size*, not a fixed time window — at low activity a log may hold more than a few hours. It is a debug convenience, not a hard time guarantee.

**Verify it yourself** (all read-only):

```bash
# Seconds — no audio is left between utterances (temp WAVs live in the run dir)
ls tmp_stt_*.wav 2>/dev/null | wc -l         # → 0

# Days — the nightly scrub/prune actually ran
grep -E 'zdr_scrub|transcript_prune' ~/Library/Logs/Marvin/feedback_batch_cron.log | tail
#   → deleted_rows: N   /   scrubbed_fields: N   /   ✅ success

# Days — old improvement signals are hashed, not readable
grep -c 'scrubbed:sha1:' records/agent_gaps.jsonl

# Hours — rotation caps are in force
ls -la bot_stdout.log* stt_history.log*

# The 03:00 job is scheduled
launchctl list | grep feedbackbatch
```

Nothing leaves your machine except the consented cloud calls above (Groq for STT, Gemini/Cerebras for responses), governed by those providers' policies. `marvin.db`, `suki_memory.json`, and `records/` are gitignored by default.

---

## Architecture

The voice pipeline lives in `marvin_voice_core/` (decoupled from bot logic, usable standalone). Wake-word intents go through a separate IntentBus where all agents bid in parallel and the max-confidence handler wins; a parallel STT judges race (regex / Groq 8B / cleaner) feeds it. Two opt-in bridges (`MarmoServer`, `CompanionBridge`) let external agents push text in and an operator control surface watch what Marvin hears and chooses.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full module map, the IntentBus bid contract, and the integration surfaces.

---

## Contributing

Code comments are in Traditional Chinese (zh-TW) — this started as a personal bot for a Taiwanese gaming group. English PRs are welcome; translating comments is appreciated but not required.

If you successfully run this on a fresh machine, please open a GitHub Discussions post in the "Show your setup" thread. That single confirmation is the most useful signal this project can receive right now.

## License

MIT
