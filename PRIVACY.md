# PRIVACY — 隱私說明（草稿）

> **草稿，尚未對外發布。** 內容依 2026-09-27 的程式碼與這台主機的實際狀態整理，「待確認」＝程式碼裡找不到、沒有猜。
>
> ⚠️ **發布前必須先處理（給營運者）**——下面這幾點讓現行程式的行為與「一般使用者會預期的隱私說明」對不上：
> 1. **保留期限目前沒有執行**：逐字稿「14 天刪除」、紀錄原文「14 天轉雜湊」的排程（launchd `feedbackbatch`）被停用，2026-07-09 之後沒跑過。資料庫裡目前有 91,993 句超過 14 天的逐字稿（最舊 2026-06-25）。
> 2. **身分用暱稱辨認，不是 Discord 帳號 ID**：同意狀態與記憶都綁「顯示名稱」。同名的兩個人會共用同意與記憶；改名會讓記憶斷掉。
> 3. **沒有按伺服器隔離**：所有伺服器的人物記憶都寫進同一個分區（見 ARCHITECTURE.md §6）。
> 4. **沒同意的人也會被記錄語音頻道進出**（帳號 ID + 名稱 + 時間），而且是 bot 所在的每個伺服器、每個語音頻道（見 §3b）。
> 5. **使用者沒辦法自己刪資料**：`/marvin_optout` 只停止之後的處理，不會刪除已存的資料。
> 6. **同意通知寫的外部服務不完整**：通知只寫 Groq 與 Google Gemini / Cerebras，實際還會送到 Mistral、SambaNova、Together、OpenRouter（見 §4）。
> 7. 聯絡方式、營運者身分、適用法律、未成年人政策：**待確認**。

---

## 1. 誰在處理你的資料

Marvin 是個人自架的 Discord bot，所有資料存在營運者的**一台 Mac**上（沒有雲端資料庫）。營運者：待確認（對外公開的名稱與聯絡方式）。

Marvin 需要你把他召喚進語音頻道（`/summon`）才會開始聽。他在語音頻道裡時，會接收**頻道內每個人**的聲音。

## 2. 同意機制（程式實際行為）

- 你**第一次**進入 Marvin 所在的語音頻道時，他會在文字頻道貼一則資料使用聲明，附「同意 / 拒絕」按鈕（`cogs/voice_controller.py:937-952`，同意通知內容見 `consent_manager.consent_notice`）。
- **沒按同意 = 不處理**：辨識出的文字在進入後續流程前就被丟掉——不存逐字稿、不進記憶、不送 LLM（`cogs/voice_controller.py:1093`）。
- 隨時可改：`/marvin_optin`（同意）、`/marvin_optout`（撤回）。
- **要注意的細節**：
  - 同意檢查發生在**語音轉文字之後**。語音轉文字在營運者的 Mac 本機進行（Apple 語音辨識），**所以沒同意的人的聲音仍會在本機被辨識一次，然後丟棄**。目前設定下這一步不會送上雲端（雲端 Groq 備援已用 `STT_SWIFT_STRICT=true` 關閉）；若營運者關掉這個設定，未同意者的語音可能被送到 Groq 辨識後才被丟棄。
  - 同意狀態以你的**顯示名稱 / 伺服器暱稱**記錄，不是帳號 ID。
  - 聲明只在第一次出現；之後以 `consent.json` 裡的紀錄為準。

## 3. 實際存了哪些資料

以下「保留期限」分兩欄：**程式設定**＝程式碼裡寫的規則；**目前實況**＝這台主機現在真正發生的事。

### 3a. 聲音

| 資料 | 存在哪 | 程式設定 | 目前實況 |
|---|---|---|---|
| 每句話的暫存錄音 | 系統暫存資料夾 `tmp_stt_*.wav` | 辨識完立刻刪除（`finally`），啟動時也會清殘留 | 同左 |
| 「最近一句」錄音 | `records/last_stt_debug.wav` | 每句話覆寫，永遠留著最新一句 | 同左 |
| 指令救援錄音（Marvin 沒聽懂、改用原始語音再判斷一次的那幾句） | `records/rescue_wav/` | 保留最近 500 個檔 | 目前 8 個 |
| 喚醒詞樣本 | `records/wake_samples/` | 只收營運者本人（`MARVIN_OWNER_ID`）喊「馬文」的錄音 | 開啟中，411 段，不會自動刪 |
| **推廣用錄音**（營運者用 OBS 另外錄，不是 Marvin 錄的） | 營運者電腦（例如 `~/Movies/Marvin錄音/`） | 錄之前會先告知在場的人；剪成短片前會先問過當事人；用不到的會刪除（`validation/RECORDING_SETUP.md`） | 2026-09 起，驗證期間 |
| 除錯封存錄音 | `records/stt_debug_*.wav` | 預設關閉；開啟時保留 7 天 | 關閉 |

### 3b. 文字與記憶

| 資料 | 內容 | 存在哪 | 程式設定 | 目前實況 |
|---|---|---|---|---|
| 逐字稿 | 誰、哪個頻道、說了什麼、時間 | `marvin.db` `transcripts` 表 | 14 天後刪除（夜間排程） | 排程待啟用（Stage D 程式已完成） |
| 社交話題圖 | 誰、說了什麼（原文）、語意向量、情緒 | `marvin.db` `speaker_topic_graph` | 30 天 | 排程待啟用（Stage D 程式已完成） |
| 語意向量庫 | 每句原文 + 語意向量 + 講者 + 伺服器 | `.chroma_db/` | 語音轉成的文字會在向量庫保留 90 天，用於長期記憶 | 排程待啟用（Stage D 程式已完成） |
| 語音辨識紀錄檔 | 每句辨識結果 | `stt_history.log`（5MB×3 輪替）、`records/daily/`、launchd 的 `bot_stdout.log` | stt_history.log 依大小汰換；每日切片保留 14 天；launchd log 每日輪替保留 14 份 | 排程待啟用（Stage D 程式已完成） |
| 人物記憶 | Marvin 對你的印象、關係階段、好惡、禁忌、音樂口味、說話風格、個人資訊（飲食/穿著/居住/交通、Minecraft ID）、印象深刻的情緒時刻、點歌紀錄、互動統計、待回應的話題 | `marvin.db` `players` 表 + `suki_memory.json` | 長期保留，沒有刪除規則 | 備份：`suki_memory.*.bak` 保留 7 份；`records/backups/` 約 140 份不會自動刪 |
| 5 分鐘對話摘要 | 摘要文字、在場者 | `marvin.db` `session_summaries` | 30 天 | 排程待啟用（Stage D 程式已完成） |
| 待辦 / 承諾 | 誰答應了什麼、原句 | `marvin.db` `tasks` | done / cancelled 30 天；pending 永久保留 | 排程待啟用（Stage D 程式已完成） |
| 日記 | 每 10 分鐘的四行日記 | Discord 頻道 `#馬文的厭世日記` + `records/chat_summary_log.txt` | Discord 上的依該伺服器管理；本機檔沒有刪除規則 | |
| 音樂紀錄 | 每首歌的點歌者與反應、每人推薦、跳過紀錄 | `music_memory.json` | 長期保留 | |
| 同意狀態 | 顯示名 → 同意 / 已看過聲明 | `consent.json` | 長期保留 | |
| 離場習慣 | 顯示名 → 何時離開、有沒有說再見 | `departure_stats.json` | 長期保留 | |
| **語音頻道進出紀錄** | 時間、伺服器 ID、**Discord 帳號 ID**、顯示名、頻道 ID、進 / 出（2026-10-01 起不再記換頻道） | `data/voice_presence.jsonl` | 只記 Marvin 所在頻道、已同意者的進出；保留 90 天 | 2026-10-01 前的舊紀錄待營運者確認後清理；90 天保留排程待啟用（Stage D 程式已完成） |
| 自我改進紀錄 | 聽不懂的句子、指令判斷結果（含原文） | `records/*.jsonl` | 14 天後原文改成 SHA-1 雜湊 | 排程待啟用（Stage D 程式已完成） |
| 馬文台詞紀錄 | 馬文每句講出來的完整內容（可能引用你說過的話）與播出時間 | `marvin_speech.log` | 依大小輪替（5MB×3） | 2026-09-27 起 |
| 系統 log | 錯誤、延遲、部分內容片段 | `bot_main.log`、`bot_stdout.log` | 依大小輪替 | |

### 3b-2. Marvin 會把存下的內容說出來

以下功能**不需要被喚醒**，會在語音頻道裡、在其他人面前講出之前的對話內容：
- 冷場時根據最近 10 分鐘的對話與個人資料生成話題並說出來。
- 你過去說過要做的事（從對話摘要中偵測），在相關話題出現時問「你之前說要……，現在呢？」
- 多人對話中偵測到的承諾，靜默後問「剛才說的『……』，要記成待辦嗎？」
- 進場招呼、DJ 口白、以及貼到 Discord 的日記。

### 3c. 不會存的

- 沒按同意的人說的話（本機辨識後即丟棄，見 §2）。
- Discord 文字頻道的一般訊息內容：只有 `@Marvin` 提及他的訊息會被處理（`cogs/voice_controller.py:883`）。`discord_temperature_monitor` 只在一個指定頻道（`TEMP_TEXT_CHANNEL_ID`）計算「有訊息」的次數，不存內容。其他模組是否有落地訊息內容：沒找到，但 bot 用了 `Intents.all()`，技術上讀得到所有訊息。

## 4. 會送到哪些外部服務

| 服務 | 送什麼 | 什麼時候 |
|---|---|---|
| **Google Gemini**（免費與付費 API） | 對話文字、人物記憶片段；**原始語音片段** | 回應、摘要、每日記憶萃取；你喊「馬文」時那段語音會送去做情緒分析（`cogs/voice_controller.py:1314`）；Marvin 沒聽懂指令時，那段語音會送去再判斷一次（`MARVIN_INTENT_RESCUE_MODE=audio`） |
| **Groq** | 對話文字 | 串流回應第一順位、LLM 池 |
| **Mistral、SambaNova、Together AI、OpenRouter** | 對話文字 | LLM 池（誰有空用誰；近 7 天 68% 的呼叫走 Mistral） |
| **Microsoft Edge TTS** | Marvin 要講的話 | 每次講話 |
| **YouTube / YouTube Music、iTunes、Spotify（只查歌曲資訊）、歌詞服務、DuckDuckGo** | 歌名、搜尋關鍵字 | 點歌、封面、歌詞、查資料 |
| **Discord** | 回覆、日記、同意通知 | — |

這些服務各自的保留與訓練政策：**待確認**（各家免費層與付費層的條款不同，發布前要逐一查證）。
未使用：Cerebras（已失效）、雅婷（已退役）、Ollama（未安裝）。

## 5. 怎麼刪除

**目前使用者沒有辦法自己刪除。** `/marvin_optout` 只會讓 Marvin **之後**不再處理你的語音，已存的資料不會消失。

要刪除請聯絡營運者（聯絡方式：待確認）。營運者手動刪除時要清的地方（以顯示名稱比對，改過名的話每個名字都要清）：

| 位置 | 做法 |
|---|---|
| `marvin.db` `players` | 停 bot 後刪除該列（bot 執行中會被記憶體快取寫回）；程式碼沒有現成的刪除 API（`TODOS.md` 有記） |
| `suki_memory.json`、`suki_memory.*.bak`、`records/backups/*` | 移除該人的項目 |
| `marvin.db` `transcripts`、`speaker_topic_graph`、`session_summaries`、`tasks`、`laugh_events`、`atmosphere_corrections` | 依 speaker / speakers / assignee 刪 |
| `.chroma_db/` | `VectorStore.delete_speaker(speaker, guild_id)`（已有，但沒有入口呼叫） |
| `music_memory.json`、`departure_stats.json`、`consent.json` | 移除該人的 key |
| `data/voice_presence.jsonl` | 依 `user_id` 刪列（這份是用帳號 ID 記的） |
| `records/daily/`、`stt_history.log*`、`~/Library/Logs/Marvin/bot_stdout.log`、`records/*.jsonl`、`records/rescue_wav/` | 純文字 / 錄音檔，需逐檔處理 |
| Discord 上的日記訊息 | 在該伺服器刪除 |

已送到外部服務的資料：依各服務政策，營運者無法代為刪除（待確認）。

## 6. 變更紀錄

- 2026-09-27：依程式碼重寫的第一版草稿。
