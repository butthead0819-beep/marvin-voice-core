# STATUS — 目前狀態

> 2026-09-27，`main` @ `bcc36ca`。依程式碼、prod 設定（`.env` + launchd `run_bot.py`）與實測結果整理。
> 「待確認」＝找不到證據，沒有猜。標「營運筆記」的是過去的營運紀錄，程式碼裡看不出來，要重新確認。

## 驗證基準

| 項目 | 結果 |
|---|---|
| 測試 | `pytest tests/`：**5963 passed、4 skipped、0 failed**（本機 Python 3.13.5，2026-09-27，4 分 22 秒） |
| CI | GitHub 帳號 2026-09-25 起停權中，push / PR / CI 全停。本機 `main` 比 `origin/main` 多 **5 個 commit 沒 push** |
| CI 與 prod 版本 | CI、Dockerfile 用 Python 3.12；prod 用 3.13.5（見 DEPENDENCIES.md §4） |
| 部署 | 單一 Mac（launchd 常駐），只服務一個主伺服器（`GUILD_ID`） |

---

## ✅ 目前可用（prod 開啟中）

**語音核心**
- `/summon` 進語音、DAVE 加密語音解密、依人切句
- 本機 STT：SpeechAnalyzer v2 主力（`STT_ENGINE_V2=true`）、v1 備援與喚醒偵測；雲端 Groq 備援關閉（`STT_SWIFT_STRICT=true`）
- 喚醒詞文字比對 + 8 秒追問窗；三道喚醒漏接防線（alt wake、VAD 寬限窗、週期補拍）
- IntentBus 約 20 個意圖 agent；同一人先來後到、不同人並行（`MARVIN_PER_SPEAKER_QUEUE=1`）
- 聽不懂的指令用原始語音送 Gemini 再判斷（Audio Rescue，`MARVIN_INTENT_RESCUE_MODE=audio`、`SHADOW=0`）
- 聊天回應：Groq → Gemini 串流；LLM Bus（8 個 provider 輪替 + 付費兜底）
- TTS：edge-tts → macOS `say` 備援；全部講話統一響度（約 −14 LUFS）＋ mixer 限幅
- 同意機制（`/marvin_optin`、`/marvin_optout`）

**記憶**
- 人物長期記憶、每日記憶萃取（daily review，每天第一次 summon 觸發 + 巡邏迴圈兜底）
- 逐字稿、向量語意回憶、5 分鐘摘要、承諾偵測與語音確認、「剛剛說的 X 是什麼」回憶
- 口味勘誤（「我沒有喜歡 X」「我喜歡什麼」）

**音樂**
- 語音點歌、拼音點歌 fast-path（`MARVIN_MUSIC_FASTPATH=1`）、`/marvin_play` 等指令
- 真人點歌優先於 autopilot（`MARVIN_REQUEST_PRIORITY=1`）
- 自動續歌（團體記憶 → 發現 → 回收三層）、依在場者狀態選歌（`MARVIN_STATE_PICK=1`）、對話關聯選曲（`ASSOCIATIVE_CURATION=on`）
- DJ 串場：歌尾口白、笑話庫、歌詞金句、生活素材
- 每首歌響度正規化、封面卡片

**社交**
- 進場招呼（依近況動態押韻）、人還在時先送客、短時間重連不重複招呼
- 每 10 分鐘日記（2026-09-26 起每輪都寫，不再 SKIP）
- **主動發話、會帶到聊天內容**（不用喚醒）：
  - 冷場話題：連續 3 分鐘冷場 → 用近 10 分鐘逐字稿 + 個人 profile 生成話題，說「最近有點安靜，…」；每段最多 3 次、冷卻 10 分鐘（`main_discord.py:409-429`、`topic_generator.py`、`discord_temperature_monitor.py`）。近 10 天 72 次成功生成
  - 記憶主題 callback：目前的對話跟在場某人過去的承諾字面重疊 → 「對了，你之前說要 X，現在呢？」（`intent_agents/memory_callback_agent.py`、`callback_delivery.py:39`）。**是 live，不是 dry-run**：`speak_outcomes.jsonl` 9/25–26 有 3 次 `tts_pushed=true`
  - 待辦確認：5 分鐘摘要偵測到多人對話中的承諾 → 靜默後問「剛才說的『X』，要記成待辦嗎？」（`cogs/voice_controller.py:2217`）。`tasks` 表只有 1 筆，實際很少觸發
- 自發漫才（`SPONTANEOUS_MANZAI=true`）

**其他入口**
- Pi 書架喇叭衛星（launchd `satellite` 常駐中）

**維運**
- 心跳信標 + 外部 probe（每 30 分，失敗 DM owner）、ErrorDispatcher
- 付費帳本與上限（每日 $0.5 / 每月 $4）

---

## 🧪 實驗中 / 影子模式 / 預設關閉

| 功能 | 狀態 | 開關 |
|---|---|---|
| 資訊真空偵測（Gap Research） | 影子模式：只寫 `records/gap_research.jsonl`、不發話 | `GAP_RESEARCH_MODE=shadow` |
| STT 備選救援（Alt Rescue） | 影子模式：只 log | `MARVIN_ALT_RESCUE=shadow` |
| 跨人橋接（BridgeAgent） | 程式 live：有人講完話、另一位在場者以前講過相似話題 → 「B 之前也說過類似的，A 你們倆要不要對一下？」（只點名、不複述內容）。9/13–9/18 多次得標，**之後沒有得標紀錄**；9/25 前沒有 `tts_pushed` 欄位，是否真的講出來待確認 | 無開關 |
| 主動話題 ProactiveTopicAgent | 會得標，但話題來源 `proactive_topics` 目前是空的（daily review 9/18 瘦身後不再產生）→ 實際上不會講 | — |
| Marmo 雙人對白 | 30% 機率 | `MARMO_DUAL_SPEAK=true`、`MARMO_DUAL_CHANCE=0.3` |
| NemoClaw（龍蝦，openclaw） | 只給 owner | `NVIDIA_API_KEY` |
| 讀空氣主題歌單 | **關** | `MARVIN_THEMED_PLAYLIST=0` |
| 串流 STT 語意斷句 | **關** | `STT_STREAMING=false` |
| Volatile / 台語 STT 影子 | **關**（量測已完成 / 雅婷已退役） | `VOLATILE_SHADOW`、`NAN_STT_SHADOW` |
| 車用 puck | Discord bot 內強制關（`run_bot.py`），由 satellite 程序負責 | `MARVIN_CAR_MODE` |
| 瀏覽器衛星 | plist 存在但**未載入** | `MARVIN_SATELLITE_BROWSER` |
| Companion Radar | 關 | `COMPANION_RADAR_ENABLED=false` |
| 自訓聲學喚醒模型（openWakeWord） | 只有 scripts 實驗，沒上線 | — |
| Spotify Connect 個人 DJ | 營運筆記：Phase 1 完成；程式碼中只找到 scripts 與 metadata 查詢，整合狀態待確認 | — |
| Linux / Docker 路徑 | 有 Dockerfile，**不維護**（TODOS：等第一個 Linux 用戶再處理） | `STT_ENGINE≠macos` |
| `/marvin_talk` 回合制對談 | 已移除（2026-09-29 刪程式碼，2026-10-01 確認 Discord 已無此指令） | — |
| Suno 生歌 | 仍可被觸發（`manual_sing_request`），但沒有記帳；Lyria 已永久關閉 | `SUNO_API_KEY` |

---

## 🐞 已知 bug / 風險

依嚴重度排序。🔴 = 開放給其他伺服器前必須處理。

| # | 問題 | 證據 | 影響 |
|---|---|---|---|
| 🔴1 | **多伺服器同時使用會互相干擾**：單一 engine / sink / 文字頻道 / 音樂佇列、voice client 取「第一個」、最後一人離開會斷開所有伺服器 | ARCHITECTURE.md §7a（12 項） | 回應可能跑到別的伺服器；一邊散場另一邊被踢 |
| 🔴2 | **記憶沒有按伺服器隔離**：prod 只有一個 `MemoryManager()`，恆寫主伺服器分區 | `gemini_router.py:177` | 跨伺服器洩漏個人記憶 |
| 🔴3 | **身分用顯示名稱**：同意、記憶、口味都綁暱稱 | `consent_manager.py:52`、`discord_voice_engine.py:1003` | 同名共用同意與記憶；改名失憶；可冒用 |
| 🔴4 | `/marvin_reboot` 沒權限檢查（會 `git pull` 並重啟，還把 pull 輸出貼回頻道） | `cogs/voice_controller.py:829` | 已修（分支 `feat/phase1-stage-a`，限 owner 且不貼 pull 輸出） |
| 🔴5 | 資料清理排程停用：`feedbackbatch.plist.disabled`，最後一次執行 2026-07-09 | `~/Library/LaunchAgents`、`feedback_batch_cron.log` | 已修：獨立每日維護排程 `com.antigravity.marvin.maintenance`（03:00，2026-10-02 啟用並完成首次清理）；向量庫 90 天清理在 bot 內，觀察後啟用 |
| 🔴6 | 語音頻道進出紀錄不看同意、涵蓋 bot 所在全部伺服器 | `main_discord.py:452` → `presence_logger.py` | 已修（分支 `feat/phase1-stage-a`，只記 Marvin 所在頻道與已同意者，無 move） |
| 🔴7 | 沒有每伺服器配額 / 同時房間上限 | OPERATOR.md §5 | 一個伺服器可用光全部免費額度與付費上限 |
| 🟠8 | `!sync` prefix 指令沒權限檢查 | `main_discord.py:626` | 不成立：`!sync` 寫在 Bot 子類別裡沒有被註冊，叫不出來（2026-10-01 實測）；死碼保留 |
| 🟠9 | 同意通知列的外部服務過時（沒寫 Mistral / SambaNova / Together / OpenRouter，還寫已失效的 Cerebras） | `cogs/voice_controller.py:941-948` | 已修（分支 `feat/phase1-stage-a`，同意通知已更新並對齊 PRIVACY.md §4） |
| 🟠10 | `psutil` 沒裝 → MemoryGuard 恆回 False，RAM 吃緊時不會跳過向量庫寫入 | `memory_guard.py:52` | 5/18 EDEADLK 事故的防護實際失效 |
| 🟠11 | STT 全 bot 一條 `Semaphore(1)` | `discord_voice_engine.py:883` | 人一多排隊延遲上升 |
| 🟡12 | launchd 的 `~/Library/Logs/Marvin/bot_stdout.log` 不輪替（215MB，含逐字稿） | OPERATOR.md §4 | 已修：每日 03:00 copytruncate + gzip，保留 14 份（含 satellite_stdout.log） |
| 🟡13 | Cerebras 已失效但 `.env` 仍有 key → Groq 串流失敗時仍會先打 Cerebras 再轉 Gemini | `gemini_router.py:163`、`gemini_router_llm.py` `stream_llm` | 備援時多一段失敗延遲 |
| 🟡14 | `macos_stt_bin`（v1，喚醒偵測用）沒進 git、也沒有寫下編譯指令 | DEPENDENCIES.md §3 | 換機器會缺喚醒偵測 |
| 🟡15 | `music_memory.json` 沒有檔案鎖，長跑腳本與 bot 同時寫會互蓋 | 營運筆記 | 口味資料遺失 |
| 🟡16 | `RealtimeVADSink.cleanup()` 在物件未完整初始化時 `__del__` 會 `AttributeError: user_buffers` | pytest 警告（`discord_voice_engine.py:742`） | 目前只看到出現在測試裡 |
| 🟡16b | 冷場話題的 LLM 失敗時，fallback 字串也會被念出來：「最近有點安靜，我想不到好話題，等一下再試」 | `topic_generator.py` 回傳 `_FALLBACK_LLM_ERROR`（非空）→ `main_discord.py:424` 照念 | 對外體驗時可能出現 |
| 🟡16c | `run_bot.py` 註解寫記憶 callback 是「dry-run 觀察」，實際是 live | `~/Library/Application Support/Marvin/run_bot.py` | 誤導 |
| 🟡17 | `error_dispatcher._inflight` 執行緒安全（罕見 race） | `TODOS.md` | 低 |
| 🟡18 | CI 用 3.12、prod 用 3.13，`audioop` 路徑 CI 測不到 | DEPENDENCIES.md §4 | 版本相關問題 CI 抓不到 |
| — | 重啟後第一首歌要等約 3 分鐘 | 營運筆記（2026-09-18），待確認 | |
| — | 喚醒漏接「跨段往前找」未做，觀察中 | 營運筆記，待確認 | |

文件與程式不一致（已在本次改寫中修正）：舊 README 說「不存原始音訊」「向量庫只存 embedding、不存原文」「每晚 03:00 清理」，都與現況不符；`docs/PLAN_B_public_bot.md` 說 consent「已 per-guild 化基礎」，程式碼沒有。

---

## 最近 20 個 commit

| Commit | 日期 | 摘要 |
|---|---|---|
| `bcc36ca` | 09-26 | daily review 加巡邏迴圈兜底觸發：AutoRejoin 或長時間不重啟時不再整天漏跑 |
| `3e4f984` | 09-26 | 拔除日記 SKIP：每輪都寫四行日記，不再帶前情提要 |
| `06ab2bf` | 09-26 | Merge：真人點歌優先 |
| `0fe9b6b` | 09-25 | 真人點歌優先於 autopilot 選曲（目前這首剩 ≥45 秒才插到最前） |
| `95200b7` | 09-25 | 點歌插入位置邏輯搬到 `queue_priority.py`（行為不變） |
| `430b102` | 09-25 | `etd_clean_reuse` logger 放行 INFO，重用命中看得到（#100） |
| `d8882a0` | 09-25 | 免喚醒分派結果寫進 `nowake_outcomes.jsonl`、納入 14 天清理、每日儀式加觀測段（#99） |
| `4b8e37d` | 09-25 | worker 重用 Semantic ETD 的 cleaner 結果，同一句話不再打兩次 LLM（#97） |
| `a2b7ec5` | 09-25 | 修 ProactiveTopic 幽靈勝出；發話紀錄加 `tts_pushed`（#96） |
| `ef49bb6` | 09-24 | 修 libopus 崩潰根因：stop 後立刻重武裝，兩條播放執行緒搶同一顆 opus encoder（#95） |
| `3047656` | 09-24 | 絕不超大音量：各音源控制 + mixer 輸出限幅（#94） |
| `ffa5bfe` | 09-24 | Marvin 所有講話統一響度（約 −14 LUFS），疊在 duck 過的音樂上（#93） |
| `f20bd1c` | 09-24 | 送客改「人還在時先送」、一小時內回來簡單招呼（#92） |
| `5dd913a` | 09-19 | 招呼語依聊天近況動態產生 2–3 句押韻口語 |
| `d894315` | 09-18 | DJ 口白過長時先拿掉「上一首」再截斷（#91） |
| `55b59fd` | 09-18 | 修：放歌時聽糊的指令被 Echo Guard 吞掉、日記停擺、重啟後音樂接不回來（#90） |
| `2cd7dff` | 09-18 | daily review 瘦身為只做玩家記憶萃取 |
| `f5df298` | 09-18 | 修關聯選曲品質閘簽章錯誤（之前從未生效）＋ 冷卻 / 逾時 / 記帳 |
| `d23265d` | 09-18 | 新增對話關聯選曲與歌詞金句 DJ（Associative Lyric DJ） |
| `804870e` | 09-18 | 新增 TasteCorrectionAgent：語音勘誤 / 查詢自己的喜好 |

整體方向：9 月下旬以「電台主持人」定位做減法打磨——音量一致、點歌優先、送客/招呼、日記穩定、減少重複 LLM 呼叫；沒有動多伺服器架構。
