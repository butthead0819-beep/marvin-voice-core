# Busted — Master Spec v1.0

## Game Concept
Multi-player Discord guessing game. One player sets a secret answer; others race to guess it from LLM-generated clues. Press the BUZZ button to lock in and answer. Being hard to guess earns the setter more points — but if nobody gets it, they lose big.

---

## State Machine
```
IDLE → JOINING → SPINNING → SETTER_INPUT → CLUE_ACTIVE → [BUZZ_LOCKED] → ROUND_RESULT → SPINNING (next) → GAME_OVER
```

### States
| State | Trigger to advance |
|---|---|
| IDLE | /Busted_start command |
| JOINING | 30s timer or host presses "Start Game" |
| SPINNING | Auto after join phase; animation ~5s |
| SETTER_INPUT | Setter submits modal OR 30s timeout (skip round) |
| CLUE_ACTIVE | New clue every 15s; max 5 clues |
| BUZZ_LOCKED | Player pressed BUZZ; 5s answer window |
| ROUND_RESULT | Correct answer OR round 5 ends |
| GAME_OVER | All players have been setter once |

---

## Player Roster
- Max 5 human players + Marvin (AI player, always participates)
- Players join via "Join Game" button during JOINING phase
- Voice-channel users are NOT auto-joined (must press button)
- `remaining_setters`: shuffled list, pop one per round, no repeats until all done

---

## Scoring

### Round Points
| Round | Guesser Score (correct) | Setter Score (if answered this round) |
|---|---|---|
| 1 | 100 | 20 |
| 2 | 80 | 40 |
| 3 | 60 | 60 |
| 4 | 40 | 80 |
| 5 | proportional | 100 |
| Nobody guesses | — | -100 |

### Round 5 Partial Scoring
```python
score = floor(100 * matching_chars / len(answer))
```
where `matching_chars` = count of character-by-character matches (positional).

Example: answer="蘋果汁" (3 chars), guess="蘋果醋" → 2 matches → score = floor(100*2/3) = 66

### Constraints
- Setter cannot earn guesser points in their own round
- Only the first correct buzzer earns the round (rounds 1–4)
- Round 5: all non-setter players submit simultaneously; all partial scores awarded

---

## Clue System

### Clue Generation (LLM)
- Primary model: `gemini_router.complete()` (full model)
- System prompt for initial clue: generate a riddle with N-char answer, give 1 clue, do NOT reveal the answer
- Each subsequent clue: "add one more hint, keep prior hints, don't give it away"
- Reveal answer character count from round 1

### Clue Embed Format
```
🎮 BUSTED — 第 N 輪 [X/5人已出題]
━━━━━━━━━━━━━━━━━━━━
🎭 出題人：@Name
🔐 答案：[N] 個字

💡 線索一：...
💡 線索二：...  ← added each round
...
⏱ 下一條線索：Xs後 | 猜中得 Y 分
━━━━━━━━━━━━━━━━━━━━
📊 積分板
@A: 120 | @B: 80 | Marvin: 60
```

---

## Buzz Button Mechanics
- Single Discord button component, always visible
- LOCKED (disabled=True): during 5s answer window after someone buzzes
- COOLDOWN: the buzzer who failed gets 30s personal cooldown (tracked in session)
  - They see the button but their buzz is silently ignored for 30s
  - Other players can still buzz during cooldown
- ROUND 5: button changes to "Submit Answer" — opens modal for all players simultaneously

### Answer Window (Rounds 1–4)
1. Player presses BUZZ → button disabled for 5s globally
2. Bot pings: "@Player has 3 seconds to answer! (voice or text)"
3. Player has 3s to:
   - Type in chat (bot reads next message from that user)
   - Or speak (STT hook delivers text via `receive_voice_answer()`)
4. If no answer in 3s → buzz fails → 30s personal cooldown → button re-enabled

---

## Spinner Animation
- 8 frames at 0.8s intervals (stays within Discord rate limits)
- Frames 1–5: cycle through player names highlighted
- Frames 6–7: slow to final 2 candidates
- Frame 8: flash winner 3× (1s gap), then display setter role

---

## Marvin as Player

### Guessing Strategy
- Model: `groq_simple_model` (llama-3.1-8b-instant) or weakest available
- Prompt: vague, self-doubting — "你是記憶力不太好的機器人，根據以下線索大膽猜一個詞，你可能猜錯沒關係"
- Buzz probability by round: 10% / 25% / 50% / 80% / 100%
- Random delay before buzzing: 1.5–4.0s (simulate thinking)
- Answers via TTS (`tts_engine.stream_audio`) + text simultaneously

### Setting Strategy (Marvin as Setter)
- Topic selection: `suki_topic_picker.pick()` → recent emotional/behavioral topics from suki_memory
- Answer selection: choose a concrete noun from the topic (e.g., topic="音樂" → answer="耳機")
- Opening line: Marvin says a personality-appropriate quip before the round begins
- Uses full LLM for clue generation (same as human setter)

---

## Data Persistence (marvin.db)

```sql
CREATE TABLE IF NOT EXISTS busted_sessions (
    session_id TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    players_json TEXT NOT NULL,      -- JSON: [{id, name}]
    final_scores_json TEXT           -- JSON: {user_id: score}
);

CREATE TABLE IF NOT EXISTS busted_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    round_num INTEGER NOT NULL,
    setter_id TEXT NOT NULL,         -- Discord user ID or "marvin"
    setter_name TEXT NOT NULL,
    answer TEXT NOT NULL,
    clues_json TEXT NOT NULL,        -- JSON: ["clue1", "clue2", ...]
    winner_id TEXT,                  -- NULL if nobody won
    winner_name TEXT,
    won_at_round INTEGER,            -- NULL if nobody won
    setter_score INTEGER NOT NULL,
    guesser_score INTEGER,           -- NULL if nobody won
    all_scores_json TEXT,            -- JSON: {user_id: score} for round 5 partial
    FOREIGN KEY (session_id) REFERENCES busted_sessions(session_id)
);
```

Marvin conversation hook: query last 3 sessions for funny moments, winners, close calls.

---

## STT Interface (Marvin Integration)

```python
# In game/engine.py — called by cogs/voice_controller.py after STT transcription
async def receive_voice_answer(user_id: int, text: str, guild_id: int) -> bool:
    """
    Returns True if text was consumed by an active buzz window.
    voice_controller.py calls this before its own processing.
    """
```

---

## Acceptance Criteria

### AC-01 Join Phase
- [ ] `/Busted_start` posts join embed with button; Marvin auto-joins
- [ ] Up to 5 humans can join; 6th press shows ephemeral "Game is full"
- [ ] Host can press "Start Game" early; 30s auto-start if ≥2 players

### AC-02 Spinner
- [ ] 8-frame animation, ~6.4s total
- [ ] Selected player is correct (random, not always same)
- [ ] Marvin included in pool

### AC-03 Setter Input
- [ ] Setter sees ephemeral modal prompt automatically
- [ ] Answer stored encrypted from embed (not shown in chat)
- [ ] If setter is Marvin: auto-picked from suki_topic_picker within 3s

### AC-04 Clue Loop
- [ ] Clue 1 appears immediately when SETTER_INPUT ends
- [ ] Clue N+1 appears 15s after clue N
- [ ] Embed shows running clue list (cumulative)
- [ ] Character count shown from round 1

### AC-05 Buzz Button
- [ ] Button always visible, disabled only during 5s global lock
- [ ] Personal 30s cooldown tracked per user (no visual change)
- [ ] Round 5 button changes label/style to "Submit Answer" → modal

### AC-06 Answer Validation (Rounds 1–4)
- [ ] LLM judges semantic correctness (not just exact string match)
- [ ] Correct → ROUND_RESULT, score awarded
- [ ] Incorrect → 30s cooldown for buzzer, button re-enabled

### AC-07 Round 5 Scoring
- [ ] All players can submit via modal simultaneously
- [ ] Partial scores calculated per formula
- [ ] Setter gets 100 if any partial > 0, else -100

### AC-08 Score Embed
- [ ] Score board updates after every round
- [ ] Always appears in the game message (bottom of embed)

### AC-09 Game Over
- [ ] Triggers after all players have been setter once
- [ ] Final embed shows leaderboard and winner
- [ ] Results saved to busted_sessions + busted_rounds

### AC-10 Marvin AI
- [ ] Marvin guesses with weak model, vague prompt
- [ ] Marvin buzz probability scales by round
- [ ] Marvin TTS + text answer when buzzing
- [ ] Marvin setter uses suki_topic_picker

### AC-11 STT Hook
- [ ] `receive_voice_answer(user_id, text, guild_id)` returns True when consumed
- [ ] voice_controller.py stub wired (no-op if no active game)

---

## File Map
```
game/
  __init__.py
  engine.py          # GameEngine class, state machine, public API
  session.py         # GameSession dataclass + PlayerState
  scoring.py         # score(), partial_score() pure functions
  clue_generator.py  # async generate_clue(answer, round_num, prior_clues) -> str
  marvin_player.py   # MarvinPlayer: auto-join, buzz logic, TTS answer
  suki_topic_picker.py  # pick() -> (topic, answer) using suki_memory

cogs/
  game_cog.py        # BustedCog: slash commands, views, embed management

agents/
  master_agent.py    # Runs spec validation via Claude API
  coding_agent.py    # Gets implementation tasks, writes code
  qa_agent.py        # Generates and runs tests
  run_pipeline.py    # Orchestrates the multi-agent pipeline
```
