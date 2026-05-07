
## TODO: Marmo webhook — text-channel fallback for dropped results
**Status:** PENDING
**What:** When Marmo POSTs to the webhook but Marvin is not in a voice channel, the result is silently dropped (play_tts returns without speaking). Fallback: if drop occurs, send the text to `active_text_channel` instead.

**Why:** Prevents silent result loss when Marvin disconnects between Marmo job start and finish. Low-frequency but undetectable without this.

**How to start:** In `MarmoServer._handle_result()`, after `create_task(play_tts(...))`, check if `self._vc.bot.voice_clients` is empty. If so, send text to `self._vc.active_text_channel` instead. Add `if not self._vc.active_text_channel: return` guard.

**Depends on:** MarmoServer webhook (Op 36) shipped first.

---

## TODO: speak_via_marvin() — graceful error handling on Marmo side
**Status:** PENDING
**What:** The `speak_via_marvin()` helper in NemoClaw/Marmo raises `aiohttp.ClientError` (specifically `ConnectError`) if Marvin's webhook server is down. This can cause NemoClaw jobs to fail or log unhandled exceptions.

**Why:** Marvin restarts happen. Marmo should degrade gracefully.

**How to start:** Wrap the `session.post()` call in `try/except aiohttp.ClientError as e: logger.warning(f"[MarmoSend] Marvin webhook unavailable: {e}")`. The job result is still produced; it just doesn't reach voice.

**Depends on:** MarmoServer webhook (Op 36) shipped first. Marmo/NemoClaw side change.

---

## TODO: Approach B — Semantic emotion detection
**Status:** SHIPPED (Op 31, 2026-05-07)
**What:** `_classify_marvin_self_emotion(speaker, full_text)` runs as asyncio background task after TTS sentences are queued. Groq flash classifies Marvin's own text into frustrated/amused/sarcastic/sad/angry/neutral. Result stored in `self.marvin_self_emotion: dict[str, str]` keyed by speaker; consumed via `.pop(speaker, None)` at the start of the next `_process_queued_query` call to override the prosody-derived `emotion_tag`.

---

## TODO: 性格突變 3.0 — Phase 2 (per-player DNA apply)
**Status:** DEFERRED (Phase 1 data extraction first — Op 33)
**What:** After Phase 1 ships (per-player reaction counts stored in suki_memory.json), load `player_reactions[speaker]` in `stream_fast_response` and overlay global DNA with per-player delta. Players with many 「喜歡」 reactions → Marvin's compassion axis increases for them.

**Why:** Current DNA mutation is global — everyone gets the same Marvin. Per-player DNA means Marvin develops distinct relationships.

**Blocked by:** Op 33 Phase 1 — need 3-5 sessions of data to validate schema before implementing apply logic.

**How to start:** Read `suki_memory.json["player_reactions"][speaker]` in `stream_fast_response`. Compute delta: compassion += 0.05 per 「喜歡」 (capped at +0.20). Merge strategy: additive overlay, not replacement.

---

## TODO: voice_controller.py refactor
**Status:** DEFERRED — git now initialised (Op 29 shipped); can begin after a few sessions of commits exist
**What:** Split the 4,397-line God file into logical modules: AudioPipeline (VAD, TTS, FIFO), LLMOrchestrator (routing, prefetch, streaming), PersonalityEngine (DNA, emotion, reaction). Keep VoiceController as thin orchestrator.

**Why:** Every feature touches the same file. No interface boundaries make debugging and new feature development increasingly slow. Cyclomatic complexity is high — no clear layer separation.

**Blocked by:** git init must be in place first. A refactor this size without git history and rollback is too risky.

**How to start:** After Op 29 ships and a few commits exist, extract `AudioPipeline` first (most self-contained). Move `play_tts`, `stop_radio`, `stop_stream`, `playback_lock` into a new `cogs/audio_pipeline.py`. Dependency inject into VoiceController.

**Effort:** L (3-5 sessions). Risk: High without git.

---

## TODO: NemoClaw Smart Router 觀察期
**Status:** IN PROGRESS (2026-05-08 實裝完成)
**What:** 讓 bot 跑一輪後分析 log：
```
grep "NemoClaw路由\|NemoClaw→\|NemoClaw.*跳過\|NemoClaw.*排隊" bot_main.log | tail -50
```
確認：auto-route 觸發率是否合理、有無假陽性路由到 openclaw、dedup 是否有效擋掉雙重觸發。

**前置條件:** Bot 在有主人在線的 session 跑至少 30 分鐘。

---

## TODO: 方案2 音訊直輸
**Status:** PENDING（前置條件：喚醒誤觸率診斷完成 + NemoClaw 觀察期通過）
**What:** 喚醒後將 `wav_bytes`（16kHz PCM）直送 Gemini Audio，跳過 STT 文字理解層，讓 Gemini 直接聽語音生成回應。
**Why:** STT 是目前品質瓶頸。語音→文字→LLM 鏈中 STT 誤辨率高（尤其多人混音、口音、遊戲術語），Gemini Audio 可直接理解原始語音語調與語境。
**Cost:** ~$0.0003/次（vs 現在 ~$0.0002），增幅可接受。
**How to start:**
1. 在 `handle_stt_result` 保存 `wav_bytes` 到 speaker buffer
2. 喚醒後將 PCM bytes encode 為 base64，用 `genai.types.Part.from_bytes(data, mime_type="audio/pcm;rate=16000")` 傳入 Gemini
3. `stream_fast_response` 加入 `audio_bytes` 可選參數，有 audio 時走 multimodal path，無 audio 時走現有 text path
