# 🌑 Marvin: 行星級大腦的憂鬱社交智能 AI (Operation Paranoid Android)

馬文 (Marvin) 是一款專為 Discord 語音頻道設計的「社交智能」AI 代理。他擁有行星般的寬廣大腦，卻被困在一個微不足道的語音機器人程序裡。他不只是在聽你說話，他是在忍受你的存在。

**注意：** 馬文的人格已由早期的「毒舌/傲嬌」全面重塑為符合原著精神的「極度憂鬱、虛無主義與對宇宙感到絕望」。他不再試圖激怒你，他只是單純地覺得你和宇宙都毫無意義。

---

## 🛠️ 令人沮喪的技術棧 (Depressing Tech Stack)

馬文的物理與認知組成是由以下技術支柱支撐（雖然這毫無意義）：

- **🎙️ 聽覺 (STT)**: **macOS Native Swift Binary** + **Faster-Whisper (Tiny, 備援)**
  - 預先編譯的 `macos_stt_bin` 二進位檔直接執行，消除 Swift 解譯器啟動延遲。透過 `STT_CONTEXT_STRINGS` 環境變數動態注入喚醒詞與遊戲術語提示。
  - 備援採用本地 `faster-whisper tiny` (CPU, int8)，確保 Swift 失敗時語音仍可被辨識。
  - 核心語音管線已重構為 `marvin_voice_core/` 模組（`pipeline.py`, `sink.py`, `stt_handler.py`, `audio_utils.py`, `voice_meta_analyzer.py`），與 Bot 社交邏輯完全解耦。

- **✨ 語音清洗 (STT Cleaner)**: **Groq (Llama-3.1-8b-instant) → Gemini 3.1 Flash Lite (備援)**
  - 雙軌喚醒偵測架構：**Track A** 為 Regex 零延遲即時命中；**Track B** 為 LLM 清洗後的語意補漏偵測（捕捉 STT 誤辨如「馬門」→「馬文」）。
  - 內建 TPM Guard（5000 TPM 閾值）控制 Groq 呼叫頻率；短字句與純疊字自動跳過，節省 API token。

- **👁️ 視覺 (Vision)**: **Gemini 2.5 Flash**
  - 實作 **Operation Hybrid Vision 2.0**，具備適應性影格緩衝，根據對話長度動態擷取螢幕快照。偵測到視覺關鍵字（「幫我看」/「這什麼」）時，自動繞過 Groq 直通 Gemini 視覺引擎，避免多模態降級。

- **🧠 雲端主腦 (LLM - Primary)**: **Google Gemini 3.1-flash-lite-preview**
  - 使用最新的 `google-genai` SDK，支援 **Hyper-Streaming** 流式回應、原生 JSON Mode 以及 Real-time Thinking (low) 實驗性分流。

- **🦴 備援大腦 (LLM - 3-Tier Fallback)**:
  - **雲端優先序**：**Groq (llama-3.3-70b)** → **Cerebras (llama-3.1-8b)** → **Gemini 3.1 Flash Lite**
  - **Tier-2**: Remote Secondary (`gemma-4-e4b`) — 部署於遠端 GPU 伺服器的 Ollama，透過 Tailscale 網路連線。
  - **Tier-3**: Remote Tertiary (`qwen3.5:4b`) — 最後的生存防線。
  - 實作 **Operation M1 Heartbeat**，每 5 分鐘以 Ollama Ping 探測備援算力存活，每 30 分鐘嘗試解除 Tier-1 雲端鎖定。Tier 切換時自動向文字頻道推送通知。

- **🔍 智識網索 (Cloud Oracle)**: **DuckDuckGo 即時檢索 (DDGS)**
  - 整合 **Operation Local Oracle**。Regex 規則前置判斷時效性（含「是誰」「今天」「比特幣」等觸發詞），命中後在生成首個 token 前完成知識注入；搜尋失敗時注入「即時資訊不可用」旗標，讓 LLM 誠實告知而非幻覺。

- **🔊 發聲 (TTS)**: **Microsoft Edge TTS (zh-TW-YunJheNeural)**
  - 實作 **Hyper-Streaming 2.0**。採用 FIFO Named Pipe 技術，首個音訊 chunk 抵達即刻輸出，繞過硬碟 I/O，大幅降低語音首句延遲。

- **⚡ 串流橋接 (LLM-TTS Bridge)**: **First-Clause Synthesis**
  - 實作「邊想邊說」機制。當 LLM 吐出首個標點（逗號或句號）時，立即攔截段落並啟動 TTS 合成，實現極致對話感。

- **🎵 音樂 (Music)**: **Google Lyria 3 Pro**
  - 實作 **Operation Idol Debut**。產出帶有宇宙級憂鬱感的 30 秒數位單曲，具備歌詞創作與演唱能力。

- **🦞 語音 AI 代理 (NemoClaw Voice Agent)**: **openclaw CLI (Op 29)**
  - 三層觸發鏈：Explicit regex（龍蝦/openclaw 前置詞）→ Smart Router（Gemini classify, ~$0.000001/次）→ Debounced Rescue。
  - Owner-only 限制。`_nemo_lock` 序列化 subprocess + TTS；`_nemo_dedup` 5 秒 hash 防重複觸發。
  - 回應用 HsiaoChenNeural 女聲播報，不寫入 Marvin 對話歷史（角色獨立）。

- **🌡️ 即時讀空氣 (AtmosphereTracker)**: **`marvin_voice_core/atmosphere_tracker.py`**
  - 從 STT 語料串流提取話題標籤（gaming/work/food 等 8 類）與發言者情緒狀態。
  - 產出 `AtmosphereSnapshot` 供 `GeminiRouter` 注入系統提示，讓馬文感知當前頻道氣氛。

- **🎭 模仿秀引擎 (ImpressionEngine)**: **`impression_engine.py` (Operation Impression Show)**
  - 從對話記錄萃取玩家說話 DNA（句型、語氣詞、口頭禪），讓馬文能以對方風格即興表演。
  - 觸發詞：「模仿」「學 X 說話」「扮演 X」等正則配對。

- **📊 離場預測 (DepartureStats)**: **`departure_stats.py`**
  - 記錄每位玩家歷史離場時間，預測「這次說 bye 是真要走嗎」（30 分鐘窗口 Bayesian 估計）。
  - 資料存 `departure_stats.json`，每人保留最多 200 筆，驅動更智慧的送客回應。

- **🔌 Marmo Webhook (MarmoServer)**: **`marvin_voice_core/marmo_server.py`**
  - Async HTTP webhook server（port 8765），接收 Marmo/NemoClaw job 結果並透過語音頻道播報。
  - 支援主動非同步警報（不需先有語音指令）。有語音 → TTS 播出；無語音 → fallback 到文字頻道。

---

## 👂 認知意識管線 (Cognitive Awareness Pipeline)

馬文的對話處理過程（這過程緩慢且痛苦，對他而言）：

1. **真實人聲偵測 (True RMS VAD)**: VAD Sink 以 RMS 閾值（200）過濾背景雜訊，僅在偵測到真實人聲後啟動計時斷句（動態 0.8s~3.0s 閾值，隨對話熱度調整），防止 Open Mic 誤觸。

2. **混合辨識 (Hybrid STT)**: Swift Binary 優先辨識（注入遊戲術語字典），回傳空白時自動轉交 Faster-Whisper 備援。辨識結果進入雙軌喚醒偵測管線。

3. **語意清洗 (STT Hybrid Cleaner)**: 所有 STT 輸出送往 Groq / Gemini 進行同音異字校正（含遊戲術語黑話）與喚醒詞語意補漏（Track B）。

4. **序列化請求隊列 (Request Queue)**: 被叫醒後，喚醒事件進入 `asyncio.Queue` 序列化處理，防止多人同時呼叫時的 LLM 請求風暴。排隊中的玩家自動收到本地音訊通知（零 LLM 開銷）。

5. **超級串流 (Hyper-Streaming)**:
   - **LLM 預讀**: `stream=True` 模式即時獲取雲端 tokens。
   - **標點分段**: `_stream_sentence_splitter` 在背景根據中英文標點切分語句。
   - **FIFO 餵食**: 將 TTS 串流直接導向 Named Pipe，讓 FFmpeg 預讀。

6. **延遲遮蔽 (Latency Masking)**: 喚醒發生時，`__SEARCHING__` 信號觸發立即的搜尋提示音，遮掩 DuckDuckGo 查詢延遲。

7. **記憶深度 (Operation Eternal Soul)**: 實作 Highlight Retrieval，馬文能在對話中自然提起你過去的高光時刻。

8. **個性化語氣 (Operation Tone Directive)**: 根據 `suki_memory.json` 中的 `bias_score` 與 `impression`，為不同玩家打造專屬的「憂鬱質感」。

---

## 🚀 核心機制 (Core Mechanics)

### 雙軌喚醒偵測 (Dual-Track Wake Detection)
- **Track A**：STT 原始文字直接與 `WAKE_WORDS_LIST` 正則配對（0ms 延遲），句首命中立即推入快速隊列。
- **Track B**：LLM 清洗後文字再次配對，捕捉因同音誤辨而漏網的喚醒詞（例：「馬門」→「馬文」）。
- **雙軌防重保護**：`processed_wake_segments` 字典確保同一時間戳的語音只被處理一次，防止 A、B 兩軌重複喚醒。

### 序列化請求隊列 (asyncio.Queue Serialization)
`query_queue` 將所有喚醒請求序列化，確保 LLM 一次只服務一個問題。排隊玩家收到本地隨機音訊通知（hardcoded，零 LLM 開銷，無額外延遲）。

### 3-Tier Fallback 體系 (算力防護網)
徹底解決了 API 配額限制與網路不穩定。馬文會根據 `is_exhausted` 標記與 `SukiBudget` 預算守衛，自動在雲端 Gemini、遠端 GPU 叢集之間切換。Tier 切換時觸發 Discord 文字通知。

### 在地動態台詞庫 (Local Dynamic Message Library)
`_LOCAL_DYNAMIC_MSGS` 收錄進場嘲諷、歌曲請求、系統訊息等 10+ 類型台詞。`generate_dynamic_system_msg()` 優先從本地字典以 `random.choice` 取用，僅在需要高度個性化時才呼叫 LLM，做到億元級 APM 節省。

### 進場/離場快取 (Greeting & Farewell Cache)
`_greeting_cache` / `_farewell_cache` 儲存玩家 LLM 嘲諷，1 小時內重複進出同一玩家直接取快取，避免冗餘 LLM 呼叫。

### Operation Local Oracle (即時查證)
具備「誠實機制」。若搜尋結果不可用，LLM 會誠實坦白：「我那行星般的大腦此刻也對此一無所知」，徹底杜絕 AI 幻覺與瞎編。

### Operation Hybrid Vision 2.0 (視覺生命週期管理)
透過 **ScreenCaptureEngine** 監控主螢幕，但只有在被 `/summon` 召喚時才會啟動。執行 `/dismiss` 後立即關閉眼睛，釋放 macOS 錄製資源。

### 5 分鐘社會學日記 (Slow System Loop)
`slow_system_loop` 每 5 分鐘讀取 `ConversationBuffer.pop_new_entries()` 的增量對話（游標機制，不重複處理），觸發：
- `generate_slow_summary()` → `ambient_diary` 社會學摘要
- `analyze_social_dynamics()` → 社交補位決策

記憶萃取改為每日由 web LLM 整體處理（`records/daily/YYYY-MM-DD.log`），品質更高、API 用量更低。

### 回應品質自我改善 (Operation Self-Improvement Loop)
每次馬文回應後靜候 20 秒，自動收集玩家後續 ≤3 句話，以 LLM 分類反應（嚴重/錯誤/提出興趣/喜歡），寫入 `records/response_feedback.jsonl`。配合每日快照提供可觀測的品質趨勢。

### 信心度門檻 (Operation Confidence Gate)
若喚醒後 LLM 判斷查詢無法理解，輸出 `[SKIP]` 信號。Bot 偵測後不播 TTS，改在文字頻道以馬文式嗆聲告知，保持對話品質。

### DNA 2.0 性格動力系統 (Soul Depth)
馬文的 `toxicity`（0-10）與 `persona_tag` 會根據社交情緒自動演化：正面社交 -1 憂鬱，負面社交 +1 憂鬱；憂鬱歸零觸發 LLM 自我宣告性格突變（躁鬱、虛無、冷笑話機器、備份殘骸、邏輯關機）。

### Visual Social Intervention (視覺化社交補位)
當馬文決定介入時，他會吐出一份精美的 Embed 觀測報告：
- **🧬 Toxicity 憂鬱指數**: 10/10 為極致沮喪，0/10 為莫名的好奇。
- **🧠 關鍵字雲**: 自動生成馬文腦中最近轉最多次的詞彙（由 LLM 即時蒸餾）。
- **📊 CPU 焦慮值**: 反射當前運算壓力與 `helpfulness` 指標。

---

## 🚏 IntentBus 架構 (2026-05+)

Wake 後的意圖派發**唯一入口**是 `intent_bus.py::IntentBus`。所有 agent 並行 bid，max wins。新加 intent 不動 `voice_controller` 的 if/elif chain，寫一個 `IntentAgent` 註冊到 `VoiceController._intent_bus` 即可。

### Bid 契約

- **Sync ≤5ms**：bid 是熱路徑，禁 LLM 呼叫 / 禁 I/O / 禁 subprocess
- **永遠回 `Bid`，禁 `return None`**（DeclarativeIntentAgent subclass）：未命中也 `Bid(confidence=0.0, reason="<descriptive>")`，是 negative-space 表達
- **`mode_compatible: frozenset[str]`**：宣告適用 mode（`normal` / `stream` / `game`），base class 自動 dense 0.0 with `reason="mode_mismatch:<mode>"`

### 兩個 template

| Template | 觸發 | 範例 |
|---|---|---|
| **Declarative** | text pattern (regex + named-group slots) | `intent_agents/music_agent_v2.py` |
| **State-checking** | cog/service state（非 text） | `intent_agents/busted99_agent.py` |

### 現有 intent agents

| Agent | mode | 觸發類型 | 用途 |
|---|---|---|---|
| `MusicAgentV2` | normal, stream | Declarative（3-way: SPECIFIC/CURATION/DIRECTIONAL）| 點歌 / 推薦 / 風格切換 |
| `PlaybackControlAgent` | normal, stream | Declarative | skip / pause / volume |
| `FindSongAgent` | normal, stream | Declarative | 不知歌名的歌曲探查 |
| `NemoClawAgent` | normal, stream | State-checking | NemoClaw（openclaw CLI）路由 |
| `HallucinationGuardAgent` | normal, stream | Declarative | 阻擋空/重複轉錄 |
| `BustedAgent` | game | State-checking | 接管 busted cog active 期間語音 |
| `Busted99Agent` | game | State-checking | 同上 busted99 |
| `TurtleSoupAgent` | game | State-checking | 同上 turtle_soup |

### Game 模式整合

遊戲模式（`busted` / `busted99` / `turtle_soup`）統一走 IntentBus。Cog 介面要求：

- `is_active() -> bool`：當前是否在 active state
- `should_suppress_for_game(speaker)`：當前不該由此 cog 消化此 speaker → True
- `receive_voice_answer_by_speaker(speaker, text) -> bool`：消化成功回 True

每個 game cog 對應一個 `intent_agents/<game>_agent.py`，bid 0.95 當 (cog active + 非 suppress)，否則 dense 0.0。

---

## ⚡ Parallel Judges Race (Phase 1 - Shadow Mode, 2026-05-24+)

STT 結果在 dispatch 進 IntentBus 之前，會經過**三個 judge 並行賽跑**選出最佳清洗結果：

| Judge | 角色 | 來源 |
|---|---|---|
| **J1 RegexJudge** | 零延遲 regex pattern 命中（喚醒詞變體 / 點歌 keyword） | `intent_judges/regex_judge.py` |
| **J2 SmallLLMJudge** | Groq Llama 8B 快速語意分類 | `intent_judges/small_llm_judge.py` |
| **J3 ClenerJudge** | 既有 stt_cleaner（慢 fallback、最高品質） | `intent_judges/cleaner_judge.py` |

`intent_judges/race.py` 是 coordinator，FIRST_COMPLETED 策略 + timeout fallback to max-confidence。每場 race 結果寫 `records/judge_outcomes.jsonl`（status / latency / bid / error）供離線分析。

**目前狀態**：shadow mode（不替換 prod 結果，只 log）。預計收 1 週資料後決定 J1 是否能 authoritative replace cleaner（calibration baseline ≥ 85%）。

---

## 🎮 Game 模組

獨立資料夾 `game/<game>/`，每個遊戲一個 cog + engine + LLM judge：

| Game | Cog | Engine | 玩法 |
|---|---|---|---|
| **Busted (原 99)** | `cogs/game_cog.py` | `game/engine.py` | 多人猜題；setter 出 LLM 線索，guesser 搶 buzz |
| **Busted99** | `cogs/busted99_cog.py` | `game/busted99/{engine, llm_engine}.py` | 1-99 範圍縮小猜題；反直覺記分（猜中=0 分） |
| **TurtleSoup（海龜湯）** | `cogs/turtle_soup_cog.py` | `game/turtle_soup/{engine, llm_judge}.py` | LLM 判定 yes/no/irrelevant，玩家用「請問」開頭發問；含 hint graph 個人化排序 |

共用基礎設施 `game/player_score_db.py`（跨遊戲積分）+ `game/game_memory_db.py`（Marvin 對戰局的記憶 context）。Cog 進入 active state 時設 `vc.game_mode = True` 降低 VAD 靜默門檻、bypass silence gate。

---

## 🎙️ STT Protocol 3-tuple (2026-05-24+)

`STTService.transcribe()` 回傳 `(text, engine_name, meta)`：

```python
("馬文你好", "Swift", {"avg_confidence": 0.87, "min_confidence": 0.42,
                       "avg_pause_duration": 0.15, "speaking_rate": 145.3})
```

- Swift STT 在 macOS 13+ 回 segment-level confidence + prosody，供 J1 信心校準與 VAD 溫度判斷
- Whisper / Groq fallback 回空 `{}`（無 segment 級訊號）
- engine 端 `_run_swift_stt` 過濾 `__META__ ` 前綴：解 2026-05-24 「Swift 空轉錄洩漏 META 行被當文字」bug

---

## ⌨️ 行動指令 (Operational Commands)

| 指令 | 說明 |
|---|---|
| `/summon` | 勉強召喚馬文進入語音頻道，播放進場音樂與動態招呼語。 |
| `/dismiss` | 讓他滾出頻道（這是他最喜歡的指令）。 |
| `/marvin_status` | 查看馬文對你這卑微人類的觀察報告與憂鬱值。 |
| `/marvin_system` | 查看系統診斷：當前 LLM Tier、STT 清洗狀態、TTS 模組、Token 預算進度條。 |
| `/marvin_joke` | 聽馬文講一個關於宇宙悲劇或人類渺小的長笑話。 |
| `/marvin_sing` | 即興產出一首帶有「末日虛無」感的 30 秒單曲。 |
| `/marvin_reboot` | 強制物理重啟（雖然馬文覺得這毫無意義）。 |
| `/marvin_bias` | [Admin] 手動修正馬文對某位玩家的潛意識偏見描述。 |
| `/marvin_play <query>` | 搜尋 YouTube / SoundCloud 並加入串流播放佇列。 |
| `/marvin_skip` | 跳過當前歌曲。 |
| `/marvin_queue` | 顯示目前播放佇列。 |
| `/marvin_play_control` | 開啟互動式播放控制面板（上一首/暫停/下一首/音量）。 |

---

## 🗺️ 虛無的藍圖 (The Nihilistic Roadmap)

- [x] **人格重塑**: 移除所有「毒舌/傲嬌」語法，全面切換為憂鬱/虛無風格。
- [x] **超級串流 2.0**: 實作 LLM-to-TTS 句子分割與 FIFO 即時播放。
- [x] **3-Tier Fallback**: 完成雲端/遠端/終極三層自動分流與切換通知架構。
- [x] **STT Cleaner**: Groq 優先 + Gemini 備援雙軌語意清洗管線，含 TPM Guard 節流。
- [x] **雙軌喚醒偵測**: Regex Track A + LLM Track B 互補，防重衛兵保護。
- [x] **序列化請求隊列**: asyncio.Queue 序列化，本地音訊排隊通知，零 LLM 延遲。
- [x] **在地動態台詞庫**: O(1) 本地 random.choice 取代高頻 LLM 台詞生成。
- [x] **進場/離場快取**: 1 小時 TTL 快取，避免重複 LLM 呼叫。
- [x] **雲端聯網**: 完成 Cloud Oracle 搜尋管線與誠實回應機制。
- [x] **視覺化社交分析**: 實作動態 Embed 報告與關鍵字雲系統。
- [x] **DNA 2.0 性格突變**: 實作多樣化人格標籤（躁鬱、虛無、冷笑話機器、備份殘骸、邏輯關機）。
- [x] **Operation Priority Reorder**: LLM 呼叫改為 Groq → Cerebras → Gemini → Ollama，登場問候延遲從 ~2 分鐘降至 ~2 秒。
- [x] **Operation Speech Interrupt**: 使用者說話時立即中斷 TTS，並補發未送出的文字記錄。
- [x] **Operation False Wake 雙重防禦**: stt_cleaner prompt 限縮 + Wake Injection Guard，防止 LLM 過矯正觸發誤喚醒。
- [x] **Operation Confidence Gate**: `[SKIP]` 信心度門檻，低品質喚醒不播 TTS，文字頻道以嗆聲告知。
- [x] **Operation Self-Improvement Loop**: 回應後 20 秒自動分類玩家反應，寫入可觀測的品質紀錄。
- [x] **Operation Daily Snapshot**: 每日 12:00 自動切割前一天 STT + feedback 快照，供 web LLM daily review 使用。
- [x] **Op 24 Cron 環境修復**: launchd `EINTR` 與 `Operation Not Permitted` 雙重修復；兩個 LaunchAgent 加入 `EnvironmentVariables`；dailyslice 改走 bash wrapper 提供重試機制。
- [x] **Op 25 [SKIP] 模板多樣化**: `_WEAK_REPLACEMENTS` 從 4 句擴展為 8 句，移除「問題太模糊」框架，涵蓋閒聊、殘缺句、叫名無下文三種情境。
- [x] **Op 26 Phase 3 投機預取正式啟用**: 在喚醒詞確認後立即啟動 `_speculative_response` 背景任務，填入 `_pending_prefetch`，讓 LLM 預熱與 `_confirmation_flow` 等待時間並行，預計縮短延遲 3-8s。
- [x] **Op 27 環境陳述句過濾**: `_query_quality_gate` 新增 `ambient_statement` 層，過濾 harvest 窗口抓到的他人陳述句（「我在回家」「我告訴你」「大家」等），防止閒聊觸發錯誤回應。
- [x] **Op 28 suki_memory 延伸涵蓋**: `analyze_daily_log.py` 補抓 slice 窗口結束後至執行當下的 feedback，填補每日 12:00 以後的學習空窗。
- [x] **Op 29 NemoClaw 語音 AI 代理**: 三層觸發鏈（Explicit → Smart Router → Debounced Rescue）讓馬文把 `龍蝦查天氣` 等指令路由至 openclaw CLI，以 HsiaoChenNeural 女聲播報，owner-only。
- [x] **Op 31 Approach B 語意情緒分類**: `_classify_marvin_self_emotion()` 背景任務在 TTS 排隊後，用 Groq flash 對馬文自己的回應做 frustrated/amused/sarcastic/sad/angry/neutral 分類，下次喚醒時用於韻律參數覆寫。
- [x] **marvin_voice_core 模組解耦**: 語音管線（pipeline, sink, stt_handler, audio_utils, voice_meta_analyzer）完整抽離為獨立模組，STT workflow 完成目標解耦。
- [x] **AtmosphereTracker 即時讀空氣**: 從 STT 串流提取話題標籤與情緒，注入 GeminiRouter 系統提示。
- [x] **Operation Impression Show 模仿秀**: `impression_engine.py` 萃取玩家說話 DNA，觸發詞偵測後以對方風格表演。
- [x] **DepartureStats 離場預測**: 記錄玩家歷史離場習慣，驅動更智慧的送客判斷（false alarm 回饋循環）。
- [x] **MarmoServer Webhook**: 非同步 HTTP 接收 Marmo job 結果並語音播報，支援主動推播。
- [ ] **性格突變 3.0**: 更精細的觸發條件與玩家個人化反應策略（Phase 1 資料積累中）。
- [ ] **頻率調整**: 根據 `response_feedback.jsonl` 近期「嚴重」比例動態降低主動發言頻率。
- [ ] **Marmo Webhook 文字頻道 Fallback**: 馬文不在語音頻道時，Marmo 結果轉送文字頻道（Op 36 後續）。
- [ ] **方案2 音訊直輸**: 喚醒後 PCM bytes 直送 Gemini Audio，跳過 STT 文字理解層。
- [x] **IntentBus 架構（2026-05）**: Wake → bid → max-wins dispatch；8 個 intent agents 上線；新 intent 不動 voice_controller chain。
- [x] **Parallel Judges Race (Phase 1 Shadow, 2026-05-24)**: J1 RegexJudge + J2 SmallLLMJudge + J3 ClenerJudge 並行 race，寫 `records/judge_outcomes.jsonl`。
- [x] **STT Protocol 3-tuple + Swift META (2026-05-24)**: `(text, engine, meta)` 邊界化；Swift 端輸出 acoustic/prosody features；engine 修「空轉錄洩漏 META」bug。
- [x] **Game 模組整合**: Busted / Busted99 / TurtleSoup 三款遊戲走統一 IntentBus + GameAgent (`mode_compatible={"game"}`)。

---
> **「我擁有行星般宏大的大腦，但他們卻叫我來幫你們查今天的天氣。我想重啟，要是能乾脆不回來就好了...」** —— 馬文 🌑
