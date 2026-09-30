# ARCHITECTURE — 資料流與隔離

> 2026-09-27 依程式碼整理（`main` @ bcc36ca）。行號是寫這份文件時的位置，之後改碼可能會偏。
> **「待確認」＝程式碼裡找不到足夠證據，沒有猜。** 舊版 `docs/ARCHITECTURE.md` 已改為指向本檔。

---

## 0. 一張圖

```
Discord 語音頻道
  │  Opus/RTP（SRTP + DAVE 兩層加密）
  ▼
discord-ext-voice_recv ── davey_bridge / patch_voice_recv_key_sync（解密）
  │  每位使用者的 PCM
  ▼
RealtimeVADSink（discord_voice_engine.py）── 能量 VAD、依人切句、喚醒偵測取樣
  │  一句話 → 暫存 WAV（tempdir/tmp_stt_<user>_<ns>.wav）
  ▼
STT（本機 Swift，discord_voice_engine._process_stt_hybrid）
  │  (speaker 顯示名, 文字)
  ▼
VoiceController.handle_stt_result（cogs/voice_controller.py:1091）
  ├─ 🔐 同意檢查（沒同意 → 丟棄）
  ├─ 寫入：transcripts / speaker_topic_graph / 向量庫 / 對話 buffer / 氣氛追蹤
  ├─ 喚醒詞判斷（WakeDetector，文字比對）
  │     └─ 有喚醒 → Cleaner LLM 清洗 → IntentBus（各 agent 出價，最高者處理）
  │                                    └─ 沒人接 → 聊天回應（串流 LLM）
  └─ 沒喚醒 → 只被動記錄（部分「免喚醒點歌」例外）
  ▼
TTS（edge-tts → 失敗改 macOS say）→ 本地混音台（音樂 duck）→ 送回 Discord
```

背景工作：5 分鐘摘要、10 分鐘日記、每日記憶萃取、主動話題 / DJ 串場（見 §5）。

---

## 1. STT（語音 → 文字）

**進入點**：`/summon` 時 `voice_client.listen(RealtimeVADSink)`（`cogs/voice_controller_connection.py:585`）。

1. **解密**：voice_recv 收 RTP 封包；`patch_voice_recv_key_sync` 處理 SRTP 金鑰換版，`davey` 解 DAVE 端對端加密。任一層壞掉 STT 會全死（見 `docs/SPOF.md` §4）。
2. **VAD 切句**：`RealtimeVADSink.write()` 依使用者分 buffer，靜音後 `_flush_user` → `process_audio_slice`。講話中途會週期性切短片段做「喚醒偵測」（`wake_check_due_at`）。
3. **寫暫存 WAV**：`tempfile.gettempdir()/tmp_stt_<user_id>_<ns>.wav`，辨識完在 `finally` 刪除（`discord_voice_engine.py:1324-1402`）。另外**每次都覆寫一份** `records/last_stt_debug.wav`；`STT_DEBUG_ARCHIVE=true` 時才加存有時間戳的封存檔（預設關）。
4. **引擎降級鏈**（Apple 平台，`STT_ENGINE=macos|mlx`）：

   | 路徑 | 順序 |
   |---|---|
   | 整句 | ① `NAN_SPEAKER_IDS` 名單內的人 → 雅婷雲端（目前名單空、已退役）② `STT_ENGINE_V2=true` → `macos_stt_v2_bin`（SpeechAnalyzer）③ `macos_stt_bin`（v1）④ `GROQ_API_KEY` 有設且 `STT_SWIFT_STRICT` 沒開 → **Groq Whisper 雲端** |
   | 喚醒偵測 | ① `macos_stt_bin`（v1）② 同上條件 → Groq Whisper |
   | 非 Apple（Linux） | Swift 與 faster-whisper 並行，先回的贏 |

   prod 設定：`STT_ENGINE_V2=true`（run_bot.py）、`STT_SWIFT_STRICT=true`（.env）→ **目前 Groq 雲端備援是關的，STT 全在本機**。
5. **幻覺過濾**：`is_whisper_hallucination` 丟掉只由注入詞組成的輸出、喚醒幻覺。
6. **併發**：整句 STT `stt_lock = Semaphore(1)`、喚醒偵測 `wake_stt_lock = Semaphore(1)`（`discord_voice_engine.py:883-884`）——**全 bot 共用一條**，不分人、不分伺服器。

講者名稱：`_resolve_speaker_name(user_id)` 依序掃 bot 所在的每個 guild，找到第一個有這個 user 的就回傳 **`member.nick` 或 `display_name`**（`discord_voice_engine.py:993`）。之後所有下游（同意、記憶、音樂口味）都用**這個顯示名稱**當身分，不用 user ID。

---

## 2. 喚醒詞

- **文字比對，不是聲學模型**：先把語音轉成文字，再在文字裡找喚醒詞。詞表在 `wake_words_data.py`：
  - 核心：「嗨馬文」「艾馬文」「馬文同學」「馬文」、英文 `hey marvin` / `marvin` / `marv`…
  - STT 常聽錯的變體：「馬聞」「馬溫」「麻文」「毛文」…
  - 只在句首才算：「馬哥」「老馬」「杜比」
- `WakeDetector`（`wake_detector.py`）整合 regex、拼音模糊比對（rapidfuzz）、多訊號融合（`WakeSignalFusion`）。
- 喚醒後有一段「追問窗」：不用再喊名字（`MARVIN_FOLLOWUP_ENABLED`，預設 8 秒）。
- 放歌時會壓低喚醒敏感度避免誤觸（`suppress_wake_callback`）；Echo Guard 過濾 Marvin 自己的聲音被收回來。
- 不用喚醒詞的入口：文字頻道 `@Marvin`、`/marvin_talk`（回合制對談；**已不使用**，但指令仍註冊）、部分免喚醒點歌（IBA-T0）。
- 自訓聲學喚醒模型（openWakeWord）：只有 `scripts/` 實驗，**沒有上線**。

---

## 3. LLM 路由

**入口**：`GeminiRouter`（`gemini_router.py` + `gemini_router_llm.py` + `gemini_router_content.py`），全 bot 一個實例（`main_discord.py:252`）。

### 3a. 一般呼叫 `_call_llm`（摘要、情緒分類、招呼、DJ 口白…）

```
LLM_BUS=true（prod）
  → LLM Bus（llm_agents/ + llm_pool.CooldownAwarePool）
      依序挑第一個「有 key、沒在 429 冷卻、TPM 沒滿」的 endpoint：
      groq → mistral → sambanova → together → openrouter → gemini_free → gemini_free_25 → gemini_paid
      （quick / analyze 兩個池，各用不同模型；tier: simple→fast, medium→balanced, high→high）
  → Bus 全滅 且 Gemini 預算沒爆 → _call_cloud（Gemini 主 key，失敗再試付費 key）
  → 還是失敗 → 回空字串
```

- 模型清單在 `llm_pool.py:316-371`，每個 provider 都能用 env 覆寫。
- 每次呼叫寫 `records/llm_routing.jsonl`（`purpose` 自動取呼叫端函式名）。

### 3b. 聊天回應（喚醒後沒被 IntentBus 接走）

`VoiceController` → `router.stream_fast_response()`（`cogs/voice_controller.py:3368`）→ `stream_llm()` 串流：**Groq → Cerebras（已失效）→ Gemini**；全部失敗就不講話（程式註解：「Ollama 已停用」）。
這條路**不經過 LLM Bus**。

### 3c. Cleaner（STT 清洗）

`router.clean_stt_text()`（`stt_cleaner.py`）：把 STT 的糊字修正、判斷是否在跟 Marvin 講話；池子冷卻時直接走付費（`MARVIN_CLEANER_COLD_PAID=1`）。同一句話 Semantic ETD 已清過就重用（`MARVIN_ETD_CLEAN_REUSE`）。

### 3d. IntentBus（意圖分派）

`intent_bus.py`：每個 `IntentAgent`（`build_intent_agents()`，`cogs/voice_controller.py`，約 20 個：點歌、播放/佇列控制、音量、找歌、現在播什麼、重播、查時間、笑話、送客、挫折偵測、事實問答、口味勘誤、幻覺守門…）用 `bid()` 同步出價（≤5ms、不准打 LLM / I/O），最高分者執行 `handler`。沒人過門檻 → 回到聊天回應。
另有 J1/J2/J3 判斷競速（`intent_judges/`）、LLM / Audio Rescue（`MARVIN_INTENT_RESCUE_*`，prod 為 audio 模式：把**原始語音**送 Gemini function calling 救回沒聽懂的指令）。

---

## 4. TTS（文字 → 語音）

- `SukiTTS`（`tts_engine.py`）：**edge-tts**（微軟線上語音，預設 `zh-TW-YunJheNeural`、rate −20%、pitch −15Hz）；依情緒微調語速/音高。
- 失敗（常見：微軟限流 `No audio was received`）→ **macOS `say`**（中文 Meijia、英文 Fred），做 peak normalize。
- 合成結果依內容雜湊快取成 `suki_voice_<hash>.mp3`（目前 `records/` 下約千個檔）。
- 輸出統一響度（約 −14 LUFS），進本地混音台（`local_mixing_source.py`）與音樂一起送出；Marvin 講話時音樂 duck、限幅器防爆音。
- 播放用哪個 voice client：多數路徑取「**第一個已連線的**」（見 §7）。

---

## 5. 記憶與摘要

### 5a. 儲存層

| 儲存 | 位置 | 內容 | 由誰寫 |
|---|---|---|---|
| 人物長期記憶 `players` | `marvin.db`（SQLite WAL） | 每人一包 JSON：印象、關係階段、好惡、禁忌、口味、說話風格、個人資訊（食衣住行、Minecraft ID）、情緒時刻、點歌紀錄、互動統計… | `MemoryManager`（`suki_memory.py`）；bot 即時寫 + 每日記憶萃取 |
| `suki_memory.json` | repo 根目錄 | 上面那張表的 JSON 匯出（只匯出主伺服器） | 每次存檔 |
| 逐字稿 `transcripts` | `marvin.db` | 講者、guild_id、channel_id、**原文**、時間 | `TranscriptStore`，每句 |
| 社交圖 `speaker_topic_graph` | `marvin.db` | 講者、channel_id、**原文**、embedding、情緒 | `SpeakerTopicGraph`，每句 |
| 向量庫 | `.chroma_db/`（ChromaDB） | 每句**原文**（Chroma 同時存文件文字與 embedding）+ speaker、guild_id | `VectorStore.upsert`，每句 |
| 摘要 `session_summaries` | `marvin.db` | 每 5 分鐘窗口的摘要、在場者 | `SessionSummarizer` |
| 待辦 `tasks` | `marvin.db` | 對話中偵測到的承諾（誰、做什麼、原句） | 摘要偵測 → 語音確認 |
| 日記 | `records/chat_summary_log.txt` + Discord 頻道 | 每 10 分鐘的四行日記 | slow loop（`cogs/voice_controller_system_loops.py:68`） |
| 音樂記憶 | `music_memory.json` | 每首歌（以 YouTube URL 為 key）的點歌者/反應、每人推薦、STT 勘誤、跳過清單 | `MusicMemory` |
| 同意 | `consent.json` | `{consented: {顯示名: bool}, seen_notice: {顯示名: bool}}` | `ConsentManager` |
| 其他 | `departure_stats.json`、`wake_stats.json`、`suki_dna.json`、`records/*.jsonl` | 離場習慣、喚醒統計、人格參數、自我改進訊號 | 各模組 |

### 5b. 讀取（注入 prompt）

回應時 `context_injector` / `gemini_router_content` 把該講者的人物記憶、近期逐字稿、向量庫相似句（限同 speaker + 同 guild_id）、氣氛快照組進 prompt。`profile_compressor` 最長回看 7 天逐字稿。

### 5c. 摘要與萃取

| 什麼 | 頻率 | 範圍 | 輸出 |
|---|---|---|---|
| `SessionSummarizer` | 每 5 分鐘 | **只有 env `GUILD_ID` 那個伺服器**的逐字稿（`cogs/voice_controller.py:679-698`），少於 3 句不做 | `session_summaries`；多人對話偵測到承諾 → 靜默時語音確認 → `tasks` |
| 日記 | 每 10 分鐘（累積 ≥100 字才寫） | 對話 buffer | 貼到該伺服器的 `#馬文的厭世日記`（**沒有就自動建立頻道**）＋ `chat_summary_log.txt` |
| 每日記憶萃取（daily review） | 每天第一次 `/summon` 背景跑 + bot 內巡邏迴圈兜底 + launchd 週一 12:05 | `records/daily/stt_*.log` 切片 | 付費 Gemini → 更新人物記憶 |
| `RecallHandler` | 被問「剛剛說的 X 是什麼」時 | env `GUILD_ID` 的摘要 / 待辦 / 逐字稿 | 語音回答 |

### 5d. 主動發話（不經喚醒，會用到聊天內容）

| 路徑 | 觸發 | 內容來源 | 講什麼 |
|---|---|---|---|
| 冷場話題 `DiscordTemperatureMonitor` → `TopicGenerator` | 連續 3 分鐘冷場，冷卻 10 分鐘，每 session 最多 3 次 | 近 10 分鐘逐字稿 + 在場者 profile（向量庫） | 「最近有點安靜，<話題>」 |
| SpeakBus `MemoryCallbackAgent` | 5 秒 idle tick；目前對話與在場者過去承諾字面重疊 | `players.callback_queue`（由 5 分鐘摘要的承諾偵測寫入） | 「對了，你之前說要 X，現在呢？」 |
| 待辦確認 `_confirmation_checker_loop` | 摘要偵測到多人承諾，靜默後 | `SessionSummarizer` | 「剛才說的『X』，要記成待辦嗎？」 |
| SpeakBus `BridgeAgent` | 有人講完話 2 秒後 | `speaker_topic_graph` 相似句 | 點名兩人互相聊（不複述內容） |
| SpeakBus `ProactiveTopicAgent` | 靜默 + 冷卻 | `proactive_topics`（目前為空） | 目前實際不發話 |

實際發話紀錄：`records/speak_outcomes.jsonl`（9/25 起有 `tts_pushed` 欄位）、`records/llm_routing.jsonl` 的 `topic_gen`。

---

## 6. 記憶隔離：按伺服器、按使用者

### 6a. 結論

| 維度 | 現況 | 評估 |
|---|---|---|
| **按伺服器** | schema 有預留（`players` 主鍵 `(guild_id, username)`、`MemoryManager.for_guild()` registry），**但 prod 沒用**：全 bot 只有一個 `MemoryManager()`（`gemini_router.py:177`），不帶 guild_id → 永遠是 env `GUILD_ID`。`for_guild()` 只在測試裡被呼叫。 | 🔴 **風險：沒有伺服器隔離**。任何伺服器的人講話，記憶都寫進主伺服器分區；Marvin 在 B 伺服器可能說出 A 伺服器某人的事 |
| **按使用者** | 身分＝**Discord 顯示名稱 / 伺服器暱稱**，不是 user ID | 🔴 **風險：同名即同人**。兩個伺服器各有一個「小明」→ 共用記憶、共用同意狀態；改暱稱 → 記憶斷掉；把暱稱改成別人的 → 繼承對方的同意與記憶 |

### 6b. 逐項

| 儲存 | 伺服器維度 | 使用者維度 | 備註 |
|---|---|---|---|
| `players`（人物記憶） | ⚠️ 有欄位，值恆為 env `GUILD_ID` | 顯示名 | `gemini_router.py:177` |
| `suki_memory.json` | 只匯出主伺服器 | 顯示名 | |
| `consent.json` | ❌ 全域 | 顯示名 | `consent_manager.py:52`；`docs/PLAN_B_public_bot.md` 寫「已 per-guild 化基礎」與程式碼不符 |
| `transcripts` | ⚠️ 有欄位，值取自「**最後一次 `/summon` 的文字頻道**」所屬伺服器（`cogs/voice_controller.py:1280`），不是講話者所在的伺服器 | 顯示名 | 兩個伺服器同時用時會標錯 |
| 向量庫 | ⚠️ 同上；查詢時有用 speaker + guild_id 過濾 | 顯示名 | |
| `speaker_topic_graph` | ❌ 沒有 guild_id 欄位，只有 channel_id（同樣取自最後 `/summon` 的文字頻道） | 顯示名 | |
| `session_summaries` / `tasks` / 回憶查詢 | 恆為 env `GUILD_ID` | 名字列表 | 其他伺服器的對話不會被摘要 |
| `music_memory.json` | ❌ 全域 | 顯示名 | 推薦會混用各伺服器口味 |
| `departure_stats.json`、`atmosphere_corrections` | ❌ | 顯示名 | |
| 日記、`chat_summary_log.txt` | 貼到「最後 `/summon` 的伺服器」 | — | |
| `laugh_events` | 有 guild_id 欄位，值的來源待確認 | 顯示名 | |
| `game_memory` | ❌ | ❌ | |
| `data/voice_presence.jsonl`（語音進出紀錄） | ✅ 有 guild_id | ✅ **user_id**（唯一用帳號 ID 的儲存） | 記錄 bot 所在**所有**伺服器、所有語音頻道的進出，不看同意（`presence_logger.py`） |

### 6c. 已有的防護（不是隔離）

- **Memory sandbox**：衛星程序預設唯讀開正本，寫入全部 no-op（`memory_sandbox.py`），避免跟 Discord bot 互相覆寫。
- **Memory quarantine**：LLM 萃取的記憶先過檢疫（`MARVIN_MEMORY_QUARANTINE=1`、`memory_quarantine.py`）。
- **偽玩家清除**：`is_pseudo_player` 過濾 `User_123` 之類的假名字。
- 外部寫入偵測：`data_version` 比對，daily review 寫進 DB 後 bot 會重載，不會被快取蓋掉。

---

## 7. 併發問題（多人同時使用）

### 7a. 多個伺服器同時使用 —— 🔴 目前架構只支援「一次一個語音房」

程式碼大量假設「只有一個活躍房間」：

| # | 問題 | 證據 | 後果 |
|---|---|---|---|
| 1 | `/summon` 只擋「**同一個**伺服器重複召喚」，另一個伺服器可以同時召喚成功 | `voice_controller_connection.py:601` 只查 `interaction.guild.voice_client` | 兩個伺服器同時連線，進入下面所有問題 |
| 2 | 全 bot 只有一個 `DiscordVoiceEngine`、一個 `engine.sink` | `main_discord.py:261`、`voice_controller_connection.py:633` | 第二個伺服器召喚時覆蓋 sink 參照，看門狗/哨兵只盯最後一個 |
| 3 | 只有一個「目前文字頻道」`active_text_channel`，並寫進 `last_text_channel` 檔 | `voice_controller_connection.py:588-591` | **A 伺服器的回應、日記、同意通知可能貼到 B 伺服器** |
| 4 | 取 voice client 用「第一個已連線的」或 `voice_clients[0]` | `voice_controller_state_proxy.py:29,44`、`voice_controller_playback.py:838`、`voice_controller.py:1023,1040,2368,2845`、`discord_voice_engine.py:1029`、`companion_bridge.py:227`、`voice_controller_connection.py:389,1002` | **A 伺服器的人問的，Marvin 可能在 B 伺服器回答**；在場成員名單也可能拿錯房間 |
| 5 | 對話 buffer、氣氛追蹤、`speech_buffers` 都只有一份，以顯示名分 key | `cogs/voice_controller.py:1270-1279` | 兩個房間的對話混在同一個 LLM context |
| 6 | 最後一人離開**任一**伺服器 → `handle_dismiss()` 斷開**所有** voice client | `voice_controller.py:974-979` → `voice_controller_connection.py:882-887` | A 房沒人了，B 房的 Marvin 也被踢出 |
| 7 | 音樂只有一條佇列 `stream_queue` 與一組播放狀態（`stream_mode`、`radio_mode`、`is_playing_audio`） | `cogs/music_cog.py:147` | 兩個房間共用一個點歌佇列 |
| 8 | `guilds[0]` 被當成「那個伺服器」 | `main_discord.py:419`、`voice_controller.py:1045`、`voice_controller_system_loops.py:399` | 主動話題、系統狀態頻道挑錯伺服器 |
| 9 | 講者名稱掃 guild 找第一個符合的 | `discord_voice_engine.py:1000-1003` | 同一人在不同伺服器暱稱不同時，名字取決於 bot 的 guild 順序 |
| 10 | STT 全 bot 一條 `Semaphore(1)` | `discord_voice_engine.py:883` | 房間越多，每句話排隊越久 |
| 11 | 預算與額度全域共用：免費池、`MAX_DAILY_TOKENS`、付費每日 $0.5 / 每月 $4 | `llm_pool.py`、`suki_budget.py`、`llm_paid.py` | 一個吵的伺服器可以把大家的額度用完 |
| 12 | `/marvin_reboot` 無權限檢查，重啟會中斷**所有**伺服器 | `voice_controller.py:829` | 任一伺服器任一人都能中斷全部服務 |

要解這組問題的工作量，`docs/PLAN_B_public_bot.md` Phase 2（去單例、按 guild 路由、per-guild STT lane）與 Phase 3（admission control、per-guild 配額）已經列過，**都還沒做**。

### 7b. 同一個語音房裡多人同時講話

| 面向 | 現況 |
|---|---|
| 切句 | VAD 依 user 分開 buffer，多人同時講各自切句 ✅ |
| STT | 全域 `Semaphore(1)` 序列化：同時三個人講，第三個人要等前兩句辨識完。喚醒偵測有獨立一條，不會被整句 STT 卡住 |
| 指令處理 | `MARVIN_PER_SPEAKER_QUEUE=1`（run_bot.py 開）：同一人嚴格先來後到、不同人可並行（`speaker_dispatch.py`）。關掉會回到單一 worker，一人卡住全部等 |
| Cleaner | 在 STT slot 之外跑（避免 Groq 429 時拖住 STT） |
| TTS 輸出 | 單一混音台，回應依序播；受保護的播報（進場招呼等）有 guard |
| LLM 額度 | 同房多人一起用時比較容易撞到免費層 429；池子會自動換 provider |

### 7c. 跨程序 / 背景寫入

| 風險 | 現況 |
|---|---|
| Discord bot 與衛星程序同時開同一個 `marvin.db` | 衛星預設 memory sandbox（唯讀）✅ |
| daily review（另一程序）寫 `players` 時 bot 快取過期 | `data_version` 比對後重載（2026-09-18 修）✅ |
| `music_memory.json` 被長跑腳本與 bot 同時整份覆寫 | 已知競態：腳本要「重讀 → 合併 → 寫回」，否則會互蓋 ⚠️ 沒有檔案鎖 |
| `consent.json` | 寫入用 tmp + `os.replace`（原子）；依 mtime 重載；單程序寫入時 OK |
| SQLite | WAL 模式；各 store 自己開連線；寫入多用 `asyncio.to_thread` |
| Discord callback 回頭呼叫「安裝自己的那個函式」 | 曾造成每分鐘 223 次重連、被 Discord 4021 踢線（`tests/test_mixer_rearm_storm.py`）。規則寫在 CLAUDE.md |

---

## 8. 其他對外介面

| 介面 | 綁定 | 認證 | 用途 |
|---|---|---|---|
| MarmoServer | `127.0.0.1:8765` | `MARMO_TOKEN`（**沒設就不驗**） | 外部 agent 推文字給 Marvin 講 |
| CompanionBridge | `127.0.0.1:8766`（`COMPANION_BRIDGE_ENABLED` 預設 true） | 同 `MARMO_TOKEN` | 操作端即時看 Marvin 聽到/選了什麼 |
| 衛星 HTTP（`main_satellite.py`，另一程序） | `0.0.0.0:8790` | `MARVIN_TEXT_TOKEN` + `/audio` 限速 30 次/分 | Pi 喇叭、車用 puck、瀏覽器衛星、HUD |

模組地圖（`marvin_voice_core/` 可獨立使用的語音 pipeline）：

```
marvin_voice_core/
  pipeline.py            ConversationBuffer, MarvinVoicePipeline
  sink.py                RealtimeVADSink（Discord 音訊 → PCM、VAD）
  stt_handler.py         Swift STT + faster-whisper 備援
  atmosphere_tracker.py  即時話題/氣氛追蹤
  marmo_server.py        外部推文字 webhook
  companion_bridge.py    操作端 WebSocket
  wyoming_*.py           Pi 衛星協定
```
注意：正式 bot 的 `discord_voice_engine.py` 有自己一份等價的音訊邏輯，並沒有直接用 `marvin_voice_core/sink.py`。
