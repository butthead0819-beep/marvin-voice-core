# OPERATOR — 維運手冊（給 Jack）

> 2026-09-27 依程式碼與這台 Mac 的實際設定整理。**「待確認」＝程式碼/設定裡找不到，沒有猜。**
> 環境變數完整清單在 `.env.example`（只收程式碼真的有讀的變數，預設值照程式碼）。

---

## 1. 環境變數

### 1a. 必填

| 變數 | 用途 | 缺了會怎樣 |
|---|---|---|
| `DISCORD_BOT_TOKEN` | Bot 身分 | `main()` 印錯誤後直接 return（`main_discord.py:668`） |
| `GOOGLE_API_KEY`（或 `GEMINI_API_KEY`） | Gemini 免費層 + LLM Bus 的 `gemini_free*` | `LLM_PROVIDER=gemini` 時 `MarvinBot.__init__` 直接 raise（`main_discord.py:247`） |
| `GUILD_ID` | 「主伺服器」。人物記憶、5 分鐘摘要、回憶查詢都寫死用它 | 預設 0 → 記憶全寫進分區 0 |

### 1b. 會送資料到外部服務的金鑰（目前 prod 有設的）

`GROQ_API_KEY`、`MISTRAL_API_KEY`、`SAMBANOVA_API_KEY`、`TOGETHER_API_KEY`、`OPENROUTER_API_KEY`、`GOOGLE_API_KEY`、`GEMINI_PAID_API_KEY`、`NVIDIA_API_KEY`、`SPOTIFY_CLIENT_ID/SECRET`、`SUNO_API_KEY`、`YATING_API_KEY`（雅婷已退役，key 還在）。
有 key 的 LLM provider 會自動進 LLM Bus 池（`llm_pool.py:316-371`），**也就是轉錄文字會送到這些服務**，PRIVACY.md 要跟著改。

### 1c. `.env` 裡有、但程式碼沒讀的（23 個，可清掉）

`BUSTED99_LLM` `CAPTURE_MONITOR` `ELEVENLABS_ENABLED` `ELEVENLABS_MODEL_ID` `ELEVENLABS_SPEED` `ELEVENLABS_VOICE_ID` `GOOGLE_APPLICATION_CREDENTIALS` `GROQ_CLEANER_MODEL` `INTERVENTION_THRESHOLD` `LLM_TIER2_MODEL` `LLM_TIER2_URL` `LLM_TIER3_MODEL` `LLM_TIER3_URL` `MARVIN_MIDSONG_HOTSWAP_ENABLED` `MARVIN_SHADOW_J2_ENABLED` `MC_CHANNEL_ID` `NVIDIA_BASE_URL` `OPENAI_API_KEY` `TTS_PITCH` `TTS_RATE` `TTS_VOICE` `VISION_ENABLED` `WEBCAM_INDEX`
（另有 `MARVIN_PUCK_BASE_URL` / `MARVIN_PUCK_TOKEN` 只給 `device/*.sh` 用；`MARVIN_INTENT_RESCUE_*`、`MISTRAL/SAMBANOVA/TOGETHER/OPENROUTER_API_KEY` 是以常數間接讀取，**有在用、不要刪**。）

`run_bot.py` 也設了三個程式碼沒讀的旗標：`PLAN12_LOCAL_MIX`、`MARVIN_SHADOW_J2_ENABLED`、`MARVIN_MIDSONG_HOTSWAP_ENABLED`，都是死設定。

### 1d. 優先順序陷阱

`main_discord.py` 用 `load_dotenv()`（不覆寫已存在的環境變數）。launchd 經 `run_bot.py` 啟動時，它**先**用 `os.environ` 設好一批值，所以：
- `run_bot.py` 用 `os.environ[...] =` 強制設的（`MARVIN_CAR_MODE=""`、`MARVIN_CAR_HARDWARE=""`）→ **`.env` 裡設 `MARVIN_CAR_MODE=1` 對 Discord bot 無效**。
- `run_bot.py` 用 `setdefault` 設的（`STT_ENGINE_V2`、`MARVIN_PER_SPEAKER_QUEUE`、`MARVIN_MUSIC_FASTPATH`、`MARVIN_ALT_RESCUE`、`NEMOCLAW_COVER`、`SPONTANEOUS_MANZAI`…）→ shell 沒設時以 `run_bot.py` 為準，`.env` 同名值被忽略。
- 手動跑 `python main_discord.py` 時沒有這一層 → **手動啟動與 launchd 啟動的行為不同**。

---

## 2. 啟動方式

程序拓撲：`launchd（com.antigravity.marvin.bot）→ /usr/bin/python3 ~/Library/Application Support/Marvin/run_bot.py → _launcher.run_with_retry() → venv_simon/bin/python3 main_discord.py`

| 情境 | 指令 |
|---|---|
| 正式（常駐） | 由 launchd 啟動：`~/Library/LaunchAgents/com.antigravity.marvin.bot.plist`（`RunAtLoad`、`WorkingDirectory=<repo>`、`ProcessType=Interactive`） |
| 重啟正式 bot | `launchctl kickstart -k gui/$(id -u)/com.antigravity.marvin.bot` |
| 看是否在跑 | `launchctl list \| grep marvin`（第一欄有 PID＝在跑） |
| 手動（開發） | `./venv_simon/bin/python main_discord.py`（沒有 run_bot.py 的旗標，見 §1d） |
| 舊的手動重啟腳本 | `./restart_bot.sh`（pkill 舊的 → nohup 起新的；**會跟 launchd 搶**，launchd 常駐時不要用） |
| 從 Discord | `/marvin_reboot`：`git pull --ff-only origin` 後 `os.execv` 原地重啟。⚠️ **沒有權限檢查**，任何伺服器的任何人都能觸發，且會把 git pull 輸出貼回該頻道（`cogs/voice_controller.py:829`、`cogs/voice_controller_connection.py:1114`） |

慣例：晚上 20–24 點非緊急不重啟（重啟會中斷正在聽歌/聊天的人）。

其他常駐 / 排程（launchd，同一個資料夾）：

| Label | 做什麼 | 排程 | 狀態 |
|---|---|---|---|
| `…marvin.satellite` | Pi 書架喇叭 / 車用 / HTTP 8790 | 常駐 | 載入中 |
| `…marvin.browsersatellite` | 瀏覽器衛星 | — | plist 存在、**未載入** |
| `…marvin.heartbeatprobe` | 外部心跳檢查，失敗用 REST DM owner | 每 30 分 | 載入 |
| `…marvin.dailyslice` | 從 `stt_history.log` 切每日逐字稿到 `records/daily/` | 每天 12:00 | 載入 |
| `…marvin.dailyreview` | 每日記憶萃取（付費 Gemini） | 週一 12:05（另有 bot 內巡邏迴圈兜底） | 載入 |
| `…marvin.speechdna` / `tasteprofile` / `tastefingerprint` / `funnelhealth` / `gmailcalendarsync` | 各種離線分析 | 各自排程（待確認細節） | 載入 |
| `…marvin.feedbackbatch` | **03:00 資料清理**：`transcript_prune`（逐字稿 14 天）、`zdr_scrub`（records 原文 14 天轉雜湊）、`golden_audit` | 每天 03:00 | ⛔ **plist 被改名為 `.disabled`、未載入**；`feedback_batch_cron.log` 最後寫入 2026-07-09 → **清理從 7/9 後沒跑過**（見 PRIVACY.md） |
| `com.marvin.control` | Mac 選單列控制 app | 登入時 | 載入 |

---

## 3. 自動重啟與監控

| 層 | 機制 | 設定 |
|---|---|---|
| launchd | `KeepAlive: {SuccessfulExit: false}`：**非 0 結束**才重拉；`ThrottleInterval 10` 秒 | bot plist |
| run_bot.py | `run_with_retry(..., max_attempts=1)`：只跑一次，失敗交回 launchd | `_launcher.py` |
| bot 自我重啟 | `self_restart()`：必定走到 `os.execv`（前置步驟都包 try/except） | `cogs/voice_controller_connection.py:~1090` |
| 事件迴圈凍住 | `liveness_beacon` 每 30 秒寫 `records/heartbeat.json`；外部 `heartbeatprobe` 每 30 分驗它有沒有過期、STT/TTS 是否活著，失敗時用 Discord REST DM owner（不經 bot 程序） | `MARVIN_HEARTBEAT`、`MARVIN_HEARTBEAT_PROBE` |
| 錯誤通知 | `ErrorDispatcher` 掛在 root logger，ERROR 以上（扣掉黑名單的噪音來源）寫 incident 並 DM owner | `main_discord.py:523` |

已知盲點：
- **程序正常結束（exit 0）launchd 不會重拉。**
- 單點故障清單見 `docs/SPOF.md`（Mac 本體、啟動鏈、event loop、DAVE 解密、Swift STT、edge-tts、單一佇列、LLM 免費池、Discord gateway）。

---

## 4. Log 位置

| 檔案 | 內容 | 輪替 |
|---|---|---|
| `<repo>/bot_stdout.log` | 所有 `print()` 與 stdout（**大部分即時診斷在這**） | 5MB × 3（`STDOUT_LOG_MAX_MB` / `STDOUT_LOG_BACKUPS`） |
| `<repo>/bot_main.log` | logging WARNING 以上 + `cogs.*` 等被放行的 INFO | 10MB × 5 |
| `<repo>/marvin_speech.log` | 馬文（與 Marmo）每句實際播出的**完整台詞** + 開始播放時間，一行一個 JSON（2026-09-27 起；給 `scripts/make_subtitle_video.py`） | 5MB × 3 |
| `<repo>/stt_history.log` | 每句 STT 結果（**含逐字稿**） | 5MB × 3 |
| `~/Library/Logs/Marvin/bot_stdout.log` | launchd 接住的 stdout/stderr（redirect 之前的輸出、launcher 訊息、崩潰 traceback） | ⚠️ **沒有輪替，目前 215MB** |
| `~/Library/Logs/Marvin/*.log` | 各排程工作的輸出（heartbeat_probe、review_cron、slice_cron…） | 無輪替 |
| `~/Library/Logs/DiagnosticReports/Python-*.ips` | 原生層崩潰（例如 libopus segfault，Python 沒有 traceback） | macOS 管理 |
| `records/llm_routing.jsonl` | 每次 LLM 呼叫：用途、provider、模型、延遲、成敗（`tokens` 欄目前一律 0） | 無 |
| `records/llm_paid_usage.jsonl` | 付費呼叫帳本（估計 USD） | 無 |
| `records/pipeline_timing.jsonl` | 語音 pipeline 各階段延遲 | 無 |
| `records/heartbeat.json` | 心跳信標 | 覆寫 |
| `records/*.jsonl`（judge / gaps / rescue / nowake …） | 自我改進訊號（含原文，應 14 天轉雜湊，但清理已停，見 §2） | 無 |

---

## 5. 限流 / 預算設定

| 項目 | 值 | 位置 |
|---|---|---|
| Gemini 主 key RPM | 12 次/60 秒（免費層上限 15，留 3 次緩衝） | `gemini_router.py:223` |
| Cleaner RPM | 12 次/60 秒 | `gemini_router.py:225` |
| Gemini 主 key 每日 token | `MAX_DAILY_TOKENS`（預設 500,000）；80% / 95% 警告，滿了斷路（`SukiBudget`，存在 `marvin.db` 的 `budget` 表） | `suki_budget.py` |
| LLM Bus 池 | 429 之後依 retry-after 冷卻該 endpoint；TPM 接近上限就跳過。Groq：TPM 6000、每日 200K token（quick/analyze 各自）；Mistral TPM 30000；其他 daily cap 未填（=不預先限制，靠 429 冷卻） | `llm_pool.py:316-371` |
| 付費兜底（`gemini_paid`、daily review 等） | **每日 $0.50、每月 $4.00**（估計值，超過就拒絕呼叫） | `llm_paid.py:55-56` |
| `/marvin_talk`（已不使用，指令仍註冊） | 每日 $2.00、每月 $10.00（另一個 guard） | `marvin_talk.py:51-52` |
| GCP 帳務 spending cap | 程式碼註解寫 $10（使用者自訂），實際值待確認（要到 GCP console 看） | `llm_paid.py` 註解 |
| STT 併發 | 整句 STT `Semaphore(1)`、喚醒偵測 `Semaphore(1)`（全 bot 共用，不分伺服器） | `discord_voice_engine.py:883-884` |
| 衛星 `/audio` | 每個 token（或 IP）每 60 秒 30 次 | `main_satellite.py:2383` |
| **每伺服器 / 每使用者配額** | **沒有**。沒有 per-guild rate limit、沒有同時服務房間數上限 | — |

---

## 6. 成本估算依據

**實際支出（付費帳本 `records/llm_paid_usage.jsonl`，估計 USD）**：

| 月份 | 估計支出 | 付費呼叫數 |
|---|---:|---:|
| 2026-05 | $2.97 | 27 |
| 2026-06 | $2.56 | 316 |
| 2026-07 | $3.99 | 2,441 |
| 2026-08 | $4.11 | 1,702 |
| 2026-09（到 27 日） | $2.94 | 643 |

近 30 天付費大宗：`daily_review` $2.78、`generate_song_jokes` $0.32、`associative_curation` $0.21、`taste_profiles` $0.15，其他都不到 $0.05。7、8 月貼著 $4/月上限，代表**上限有在擋**，不是需求只有這麼多。

**免費層吃掉大部分量**：近 7 天（`llm_routing.jsonl`）5,686 次 LLM 呼叫，約 700 次/天：Mistral 68%、Gemini 免費 19%、Groq 11%、OpenRouter 1.4%，失敗 0.3%。用量最大的是 `generate_dynamic_system_msg`、`summarize_window`（5 分鐘摘要）、`_classify_mood`、`_analyze_song_reactions`。
→ **現在的 $3–4/月建立在「一個家用伺服器 + 免費層沒被打爆」上**。免費層額度是跨所有伺服器共用的，伺服器一多就會先撞 429、品質下降，然後才開始花錢。

**不花錢的部分**：STT 走本機 Swift（免費）；TTS 走 edge-tts（免費，但會被微軟限流）；語音備援 macOS `say`。

**沒進帳本、金額待確認**：
- Suno 生歌（`SUNO_API_KEY`，`cogs/voice_controller_social.py:363` 仍可觸發 `manual_sing_request`）—— 沒有記帳。
- NVIDIA / openclaw（NemoClaw，owner 專用）—— 計費方式待確認。
- 免費層每次呼叫的 token 數：`llm_routing.jsonl` 的 `tokens` 一律記 0，**無法從 log 算出「如果全付費要多少錢」**。
- 硬體：一台 Mac mini M1 8GB（`docs/SPOF.md`）；電費 / 網路：待確認。

**開放給別的伺服器後的估算**（`docs/PLAN_B_public_bot.md` §1，2026-06-06 的估算，token 數是推估）：若全部走付費 Gemini 2.5 Flash，約 **$20 / 活躍伺服器 / 月**（保守抓 $30），主要成本是主回應每輪重送的大 system prompt。Plan B 自己設的啟動條件之一就是「成本誰付要先想清楚」。

---

## 7. 開放給其他伺服器前，維運面要先處理的（依程式碼）

1. `/marvin_reboot`、`!sync` **沒有權限檢查**（任何人可重啟 bot / 觸發指令同步）。
2. 同一時間只能正確服務**一個語音房**（見 ARCHITECTURE.md §7）。
3. 資料清理排程停了（§2 feedbackbatch）。
4. 沒有每伺服器配額，一個伺服器就能吃光共用的免費額度和付費上限（§5）。
5. `Intents.all()`（`main_discord.py:236`）需要三個 privileged intent；bot 超過 100 個伺服器時要經 Discord 審核，屆時要說明用途。
