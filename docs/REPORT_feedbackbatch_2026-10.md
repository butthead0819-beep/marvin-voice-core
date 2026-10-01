# Phase 1 資料清理與儲存保留調查報告（2026-10）

> 依 `docs/PLAN_phase1_trust_privacy.md` Stage B 要求產出，供營運者（Jack）決策參考。
> 本階段**未刪除任何資料、未啟用任何排程**，僅產出唯讀調查、建議與工具。

---

## B1. 資料清理排程為什麼停了（STATUS 🔴5）

### 1. 停用事實與歷史追查
- **排程檔案**：`~/Library/LaunchAgents/com.antigravity.marvin.feedbackbatch.plist.disabled`
- **最後執行時間**：2026-07-09 03:10:10（見 `~/Library/Logs/Marvin/feedback_batch_cron.log`）。
- **Git 紀錄事證**：
  - 在 commit `de33b65`（2026-07-17 *fix(ops): 看門狗盯產物不盯備援 log + feedbackbatch 退役除役*）中，明確記載：
    > 「feedbackbatch 2026-07-09 已人為退役（plist 改 .disabled、產物斷在 7/8）→ 從 CHECKS 移除，別再營。plist 保留不動。」
  - 在此之前的 commit `1ecc433`（2026-07-08）中提到：
    > 「launchd 日排程 07-06 後靜默停 fire（runs=1、Mac 醒著非睡眠）、看門狗 50h 才抓到。使用者訂改當天第一次 summon 觸發＝穩（跑在可靠 bot 進程）+ 即時可見成敗。」
  - 在 `docs/AGENT_MEMORY.md` 條目 `daily_feedback_ritual`（第 148 行）記錄：
    > 「2026-05-25 audit 發現 feedbackbatch 03:00 跑出來的 audit + summary markdown 沒有任何程序讀……等於每天花 LLM 算力跑分析但結果沒接起來改善……」
- **停用確切原因**：
  2026-07-09 營運者手動將 plist 重新命名為 `.disabled` 除役，主要原因為 launchd 日排程易靜默失效（脆弱性）且產出的 feedback 分析 markdown 長期缺乏下游消費程序（花費 LLM 算力卻成資訊黑洞）；但在除役 `run_feedback_batch.py` 時，未將其中掛載的 `scrub_improvement_raw.py` 與 `prune_transcripts.py` 資料清理邏輯獨立拆出，導致 14 天 ZDR 清理與逐字稿修剪一併中斷。

---

### 2. `run_feedback_batch.py` 步驟清單與特性分析

| 步驟 | 腳本與參數 | 執行內容 | 是否會改/刪資料 | 是否呼叫付費 API |
|---|---|---|:---:|:---:|
| 1 | `scripts/analyze_daily_feedback.py <yesterday>` | 分析昨日點歌回饋，寫入 `music_memory.json` 與產出 markdown 報告 | 會修改 `music_memory.json` 分數 | 是（走 LLM pool / paid fallback） |
| 2 | `scripts/analyze_latency_breakdown.py` | 統計過去 24h 各段延遲，產出 `records/latency_breakdown_<date>.md` | 否（僅產出報表） | 否（零 LLM） |
| 3 | `scripts/analyze_pipeline_timing.py` | 分析端到端管線延遲分段，更新 `records/pipeline_timing_report.md` | 否（僅產出報表） | 否（零 LLM） |
| 4 | `scripts/build_music_catalog.py --kkbox 100 ...` | 重建 fast-path 歌單與藝人關聯，寫入 `records/music_catalog.json` | 會覆寫更新 catalog JSON | 否（呼叫 KKBOX/YouTube API，無付費 LLM） |
| 5 | `scripts/analyze_llm_purpose_breakdown.py` | 統計各 purpose 之 LLM 呼叫次數與失敗歸因，產出報表 | 否（僅產出報表） | 否（零 LLM） |
| 6 | `scripts/audit_golden_dataset.py` | 檢驗 suki_golden_dataset 資料品質與 schema 變體 | 否（唯讀） | 否（零 LLM） |
| 7 | `scripts/scrub_improvement_raw.py` | 將 >14 天的 judge/gaps/rescue/nowake 原文轉為單向 SHA-1 雜湊指紋 | **會改寫**（4 個 JSONL 檔案） | 否（純本機雜湊） |
| 8 | `scripts/prune_transcripts.py` | 刪除 `marvin.db` 中 `transcripts` 表超過 14 天之逐字稿原文 | **會刪除**（SQLite DELETE） | 否 |
| 9 | `scripts/analyze_gap_research.py` | 統計免喚醒 shadow 偵測成效 | 否（僅產出統計） | 否（零 LLM） |

---

### 3. 會刪除/改寫資料步驟之 Dry-Run 檢視

- **`scripts/scrub_improvement_raw.py`**：
  - **狀態**：**沒有 dry-run，未執行。**（腳本無 `--dry-run` 旗標，若執行會直接原子覆寫 JSONL）。
  - **唯讀查詢現況統計（供參考）**：
    - `records/agent_gaps.jsonl`：總筆數 218，超過 14 天筆數 197，最舊日期 2026-05-27
    - `records/judge_outcomes.jsonl`：總筆數 1,322，超過 14 天筆數 1,210，最舊日期 1970-01-01（測試/初始化殘留）
    - `records/rescue_outcomes.jsonl`：總筆數 151，超過 14 天筆數 127，最舊日期 2026-05-28
    - `records/nowake_outcomes.jsonl`：總筆數 28，超過 14 天筆數 0

- **`scripts/prune_transcripts.py`**：
  - **狀態**：**沒有 dry-run，未執行。**（腳本無參數，呼叫即執行 `DELETE FROM transcripts WHERE timestamp < ?`）。
  - **唯讀查詢現況統計（供參考）**：
    - `marvin.db` `transcripts` 表超過 14 天之筆數：**100,199 筆**
    - 最舊真實日期：**2026-06-25T15:07:12Z**（另有極少數早期 fixture 測試殘留 1970 年戳記）

---

## B2. `bot_stdout.log` 輪替提案（STATUS 🟡12）

### 現況
- 檔案路徑：`~/Library/Logs/Marvin/bot_stdout.log`
- 目前大小：約 **218 MB**
- 產生來源：`com.antigravity.marvin.bot.plist` 中由 launchd 設定 `StandardOutPath` 與 `StandardErrorPath` 直接指向該檔。包含歷史逐字稿與例外印出。

### 關鍵問題評估
1. **launchd 開檔模式**：
   - 依據 Darwin launchd 原始碼，launchd 對於 `StandardOutPath` 是以 `O_WRONLY | O_CREAT | O_APPEND` 開啟檔案描述符。
   - **安全結論**：因為使用 `O_APPEND`，每一次寫入都會原子定位到當時的檔案末端。若外部執行截斷（例如 `truncate -s 0` 或 `> file`），後續的 write 會無縫從新檔頭（offset 0）接續寫入，不會造成稀疏檔案（sparse file）或檔案描述符損毀。因此 **`copytruncate` 在 launchd 架構下是安全的**。
2. **輪替機制選型比較**：
   - **方案 A（newsyslog）**：需要 root / `sudo` 權限在 `/etc/newsyslog.d/` 建立設定，且 macOS 系統更新時有被覆蓋的可能。
   - **方案 B（03:00 使用者排程 copytruncate，推薦）**：完全運行在 Jack 的使用者權限下，無須 root，可納入夜間維護腳本或獨立 launchd 排程（或 logrotate 本機跑）。在凌晨 03:00 低活躍時段執行 `copytruncate`，競態遺失 log 的機率極低。
3. **保留設定建議**：
   - **保留份數**：建議保留 **14 份**（每日輪替一份並 gzip 壓縮，例如 `bot_stdout.log.1.gz` 到 `bot_stdout.log.14.gz`）。
   - **保留期限理由**：對齊專案整體 ZDR 14 天時效，超過 14 天的歷史逐字稿 log 自動淘汰，兼顧隱私與除錯追查。

---

## B3. 沒有刪除規則的資料：保留期限建議

| 資料項目 | 存在位置 | 目前大小 / 筆數 | 誰在讀它（程式碼讀取點） | 建議保留期限與理由 |
|---|---|---|---|---|
| **社交話題圖** (`speaker_topic_graph`) | `marvin.db` | 156,390 筆 | `speaker_topic_graph.py`（`BridgeAgent` 用於尋找搭橋話題、`recent_speaker_history`、`find_related_turns`） | **建議保留 14 天**。<br>理由：`BridgeAgent` 的搭橋機制是為「近期在場者」尋找共通話題，數月前的對話早已失去當下社交破冰脈絡；且該表存有完整原文，對齊 14 天 ZDR 可大幅縮減 DB 體積並降低隱私風險。 |
| **對話摘要** (`session_summaries`) | `marvin.db` | 4,404 筆（約 5.5 個月） | `summary_store.py`（`recall_handler.py` 語音查詢回憶，預設 `hours=24`；`session_summarizer.py`） | **建議保留 30 天**。<br>理由：即時語音查詢（`recall_handler`）預設只回溯 24 小時；保留 30 天已足夠涵蓋「上週聊了什麼」等跨週情境，其餘長期記憶已萃取至人物 profiles。 |
| **待辦與承諾** (`tasks`) | `marvin.db` | 1 筆 | `task_store.py`（`/marvin_task`、`recall_handler.py` 待辦查詢與完成標記） | **建議「未完成永久保留，已完成保留 30 天」**。<br>理由：待辦若未完成不應隨意清除；已完成（status='done'）的任務保留 30 天供回顧後即可清理。 |
| **語意向量庫** | `.chroma_db/` | 395 MB | `vector_store.py`（`VoiceController`、`TopicGenerator`、`ContextInjector`） | **建議對話記錄保留 30–60 天，人物 Profiles 永久保留**。<br>理由：ChromaDB 內存有 `conversation_history`（每句原文與 embedding）與 `user_profiles`。原文長期沉積是隱私與磁碟負擔，可定期清理舊對話 embedding；但人物 profile 是核心個人化體驗，應予以保留。 |
| **每日語音辨識切片** | `records/daily/*.log` | 428 個檔案，36 MB | `cogs/voice_controller_system_loops.py`（每日切片寫入）、`daily_review`（只讀前一天切片）、離線研究腳本 | **建議保留 14 天**。<br>理由：daily_review 僅需前一日的切片；14 天緩衝足以應付週末補跑或除錯，無須無限期保留自 2026-04 以來的全部明細。 |
| **語音頻道進出紀錄** | `data/voice_presence.jsonl` | 4,330 行，1.1 MB | `scripts/phase1_analyze_baseline.py`、`scripts/backfill_departure_cues.py` | **建議保留 90 天（或滾動保留 30 天）**。<br>理由：Baseline 指標分析（P7 在線時長/回流率）最長觀察視窗為 30 天滾動平均，90 天足供季度對照；且 Phase 1 之後已限定 Marvin 頻道與同意者。 |
| **指令救援錄音** | `records/rescue_wav/*.wav` | 10 個檔案，5.6 MB | `intent_agents/rescue_classifier.py`、`scripts/replay_audio_rescue.py` | **維持目前程式設定（最新 500 個檔，約 250MB）**。<br>理由：`RescueWavStore` 內部已實作 FIFO 自我修剪機制，且目前僅 10 檔，容量受控，亦是模型音訊調優的重要真實語料。 |

---

## B4. 付費成本報表（1h 第一步，唯讀）

新增腳本：`scripts/paid_cost_report.py`（單元測試：`tests/test_paid_cost_report.py` 全綠）。
該腳本唯讀讀取 `records/llm_paid_usage.jsonl`，依 `Asia/Taipei` 時區切日，按月聚合總成本、呼叫次數、Token 數與前 10 大 Caller 排行。

### 實際資料輸出範例（2026-09 全月）
```text
============================================================
💰 付費 LLM 成本月報 — 2026-09 (時區: Asia/Taipei)
============================================================
當月總估算費用: $3.3719 USD
當月總呼叫次數: 789
當月總 Token 數: 5,462,139
------------------------------------------------------------
📅 每日花費分組:
  2026-09-01: $0.0672 USD
  2026-09-02: $0.1342 USD
  ...
  2026-09-30: $0.1052 USD
------------------------------------------------------------
🏆 Caller 費用排行 (Top 10):
   1. daily_review                 $2.8533 USD |   53 calls | 4,318,199 tokens
   2. associative_curation         $0.2965 USD |  263 calls |  540,081 tokens
   3. taste_profiles               $0.1480 USD |  145 calls |  146,750 tokens
   4. marvin_reply_fallback        $0.0403 USD |  249 calls |  351,255 tokens
   5. song_card_ingestion          $0.0154 USD |   12 calls |   10,730 tokens
   6. audio_rescue                 $0.0081 USD |   48 calls |   79,356 tokens
   7. paid_review                  $0.0069 USD |    8 calls |    9,520 tokens
   8. ambient_qa                   $0.0014 USD |    4 calls |    2,536 tokens
   9. audiophile_guide             $0.0013 USD |    2 calls |    1,330 tokens
  10. marvin_talk                  $0.0008 USD |    5 calls |    2,382 tokens
============================================================
```
- **核心觀察**：9 月份總付費成本約 **$3.37 USD**，其中 **84.6%（$2.85 USD）** 集中於 `daily_review`（每日記憶萃取）；目前主群單月成本遠低於原先預估的 $6–20 美金。

---

## Claude Code 驗收註記（2026-10-01）

核對程式碼與 prod 資料後，以下說法需要更正或補充，以本節為準：

1. **B2 launchd 開檔模式**：已實測確認。bot 執行中的程序對 `~/Library/Logs/Marvin/bot_stdout.log` 的 fd 旗標為 `AP`（append），copytruncate 安全。（報告原文引用「Darwin launchd 原始碼」未附出處，以實測為準。）
2. **B3 社交話題圖**：建議改為 **30 天**，不是 14 天。`speaker_topic_graph.py` 的搭橋查詢預設 `window_days=30`，保留 14 天會改變 BridgeAgent 行為。
3. **B3 語意向量庫的描述有誤**：`.chroma_db/` 只有一個 collection `marvin_transcripts`（15.5 萬筆逐字稿原文），不存在 `conversation_history`／`user_profiles` 兩個 collection；人物 profile 存在 `marvin.db` 的 `user_profiles` 表。metadata 只有 `speaker`、`guild_id`，沒有時間欄位，但 doc_id 格式為 `<speaker>_<guild_id>_<毫秒時間戳>`，可依時間清理。注意：只清 `transcripts` 表而不清向量庫，同一批原文仍長期留在向量庫；但向量庫也是長期語意記憶來源，保留天數是產品取捨，由 Jack 決定（Claude Code 建議 90 天）。
4. **B1 第一次補跑前**：`scrub_improvement_raw.py`、`prune_transcripts.py` 都沒有 dry-run，第一次執行不可逆（約 10 萬筆逐字稿、約 1,500 筆紀錄原文）。須先備份 `marvin.db` 與 4 個 jsonl、補 dry-run 並經 Jack 確認。新排程要納入看門狗盯產物（2026-07-06 launchd 曾靜默停 fire）。
5. **A3 歷史進出紀錄**：dry-run 數字正確（4,331 行；bot 1,966、move 1、未同意 0），但保留的行裡有 12 行不在 Marvin 的頻道（「這裡沒有馬文」10、「小房間」2）。`prune_presence_log.py` 需先補「只保留 Marvin 自己進過的頻道」過濾（依 bot 行判斷，須在丟棄 bot 行之前算），再 `--apply`。
