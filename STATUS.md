# STATUS — 目前狀態

> 2026-10-11，`main` @ `cf81620`。依程式碼、prod 設定（`.env` + launchd `run_bot.py`）與實測結果整理。
> 「待確認」＝找不到證據，沒有猜。標「營運筆記」的是過去的營運紀錄，程式碼裡看不出來，要重新確認。

## 驗證基準

| 項目 | 結果 |
|---|---|
| 測試 | `pytest tests/`：**6470 passed、3 skipped、0 failed**（本機 Python 3.13.5，2026-10-11，3 分 4 秒） |
| CI | GitHub 帳號 2026-09-30 解鎖，PR / CI 恢復。PR 節奏自我限制一天 ≤ 1–2 個。本機 `main` 比 `origin/main` 多 7 個 commit（事件記憶拼圖制、DJ 拔 revival），已部署、PR 待開 |
| CI 與 prod 版本 | CI、Dockerfile 用 Python 3.12；prod 用 3.13.5（見 DEPENDENCIES.md §4） |
| 部署 | 單一 Mac，launchd 常駐**一個** bot 進程（`com.antigravity.marvin.bot`），只服務一個主伺服器（`GUILD_ID`）。2026-10-08 起 satellite 進程退役，重啟只需重啟 bot |

---

## ✅ 目前可用（prod 開啟中）

**語音核心**
- `/summon` 進語音、DAVE 加密語音解密、依人切句
- 本機 STT：SpeechAnalyzer v2 主力（`STT_ENGINE_V2=true`）、v1 備援與喚醒偵測；雲端 Groq 備援關閉（`STT_SWIFT_STRICT=true`）
- 喚醒詞文字比對 + 8 秒追問窗；三道喚醒漏接防線（alt wake、VAD 寬限窗、週期補拍）
- IntentBus 意圖 agent；同一人先來後到、不同人並行（`MARVIN_PER_SPEAKER_QUEUE=1`）。2026-10-08 起意圖判斷以 LLM 為主，新 agent 必填 manifest 描述；網路查詢類問題走 Gemini grounded 搜尋
- 聽不懂的指令用原始語音送 Gemini 再判斷（Audio Rescue，`MARVIN_INTENT_RESCUE_MODE=audio`）
- 聊天回應：Groq → Gemini 串流；LLM Bus（Groq、Gemini 免費、Mistral、Together、OpenRouter 輪替 + Gemini 付費兜底）。使用者聽得到的生成排除 Mistral 小模型
- TTS：edge-tts → macOS `say` 備援；全部講話統一響度（約 −14 LUFS）＋ mixer 限幅；TTS／音樂音量平衡可在播放控制台調
- 同意機制（`/marvin_optin`、`/marvin_optout`）
- 人格固定一套 preset（2026-10-09 拔除憂鬱指數、DNA 突變、每日人格排班）

**記憶**
- 人物長期記憶、每日記憶萃取（daily review，每天第一次 summon 觸發 + 巡邏迴圈兜底）
- 逐字稿、向量語意回憶、5 分鐘摘要、承諾偵測與語音確認、「剛剛說的 X 是什麼」回憶
- 口味勘誤（「我沒有喜歡 X」「我喜歡什麼」）
- **事件記憶**（2026-10-10 起）：每個摘要窗口用 LLM 抽「誰、做什麼、哪天、哪裡」，本地驗證（原句要在逐字稿裡、主詞「我」才算說話者本人，家人/朋友的事不收），跨窗/跨天合併補欄位；誰＋做什麼＋哪天湊齊才可用。
  - 問了能答（`EventRecallAgent`）：LLM 只挑事件 id，回答由模板念資料庫欄位，不念地點
  - 事件過後追問：事件日後 14 天內、本人在場、在場者都在 `heard_by` 裡 → 「你之前說 M 月 D 日要 Y，後來怎麼樣？」，每件只問一次；播歌時走 DJ 口白，沒播歌時走主動發話。取代舊的 callback_queue
  - 從聊天學老家（`hometowns` 表）；保留 30 天

**音樂**
- 語音點歌、拼音點歌 fast-path（`MARVIN_MUSIC_FASTPATH=1`）、`/marvin_play` 等指令；「重播 X」＝點歌 X
- 真人點歌優先於 autopilot（`MARVIN_REQUEST_PRIORITY=1`）
- 自動續歌：T1 一組三首（懷舊 → 過門 → 新歌）、團體記憶 → 發現 → 回收三層、依在場者狀態選歌、對話關聯選曲
- DJ 口白：
  - 註冊表＋規劃器（`dj_narration_orchestrator.py`）決定 mode：memory_match、song、life、interest、news、conversation、perspective、callback、reason、quick 等
  - 純觀察者語氣（不准有「我」），付費 Gemini 3.1 flash-lite 優先（約 $0.02／天）
  - 懷舊位、或歌尾點火時正在熱聊（2 人以上、3 分鐘內 ≥ 20 句）→ 不講話，兩首直接 crossfade（2026-10-11 起，取代 revival 與「下一首，X 的 Y」短版）
  - 每段口白寫 `records/dj_narration.jsonl`，skip 歸屬寫 `song_plays`
- 每首歌響度正規化、歌曲卡＋導聆、封面卡片

**社交**
- 進場招呼（依近況動態押韻）、人還在時先送客、短時間重連不重複招呼
- 每 10 分鐘日記：照常產生、寫進 `records/chat_summary_log.txt` 給 DJ 用；**2026-10-02 起不再貼到 Discord**
- 冷場話題：連續 3 分鐘冷場 → 用近 10 分鐘逐字稿 + 個人 profile 生成話題（`topic_generator.py`）；每段最多 3 次、冷卻 10 分鐘
- 待辦確認：5 分鐘摘要偵測到多人對話中的承諾 → 靜默後問「剛才說的『X』，要記成待辦嗎？」（`cogs/voice_controller.py:2082`），實際很少觸發
- 自發漫才（`SPONTANEOUS_MANZAI=true`）

**車載（car puck）**
- `:8790` 車載 app 已搬進 Discord 進程（`car_http_app.py`），車上聽的是 Discord mixer 的輸出（`/audio_stream`）
- 裝置上線且 bot 不在頻道 → 自動進頻道；下車且頻道沒人 → 撤離
- 裝置自帶身分（ESP32＝showay、Pi Zero＝狗與露），多人同車
- ESP32 藍牙發射板：上車自動連車機，BMW／Lexus 已配對

**維運**
- 心跳信標 + 外部 probe（每 30 分，失敗 DM owner）、ErrorDispatcher
- 付費帳本與上限（每日 $0.5 / 每月 $4）；`_is_paid` 判斷 bug（2026-07 起付費兜底未記帳）已於 2026-10-10 修正
- 每日 03:00 維護排程（`com.antigravity.marvin.maintenance`）：資料保留清理、log 輪替

---

## 🧪 實驗中 / 預設關閉

| 功能 | 狀態 | 開關 |
|---|---|---|
| Marmo 雙人對白 | 30% 機率 | `MARMO_DUAL_SPEAK=true`、`MARMO_DUAL_CHANCE=0.3` |
| NemoClaw（龍蝦，openclaw） | 只給 owner | `NVIDIA_API_KEY` |
| DJ perspective mode | 熱聊命中 `personas/dj_perspectives.yaml` 關鍵詞才進扭蛋；2026-10-11 一天只觸發 1 次，效果待觀察 | — |
| 主動話題 ProactiveTopicAgent | 會得標，但話題來源 `proactive_topics` 目前是空的 → 實際上不會講 | — |
| 跨人橋接 BridgeAgent | `bridge_agent.py` 還在，但沒有任何地方 import，實際上沒在跑 | — |
| Linux / Docker 路徑 | 有 Dockerfile，**不維護** | `STT_ENGINE≠macos` |

**已移除**（不要再找）：`/marvin_talk`、Suno 生歌、Gap Research／Alt Rescue／Volatile／台語 STT 四個 shadow、讀空氣主題歌單與故事弧線、串流 STT 語意斷句、companion bridge／radar、SambaNova（2026-09-29）；家用 Pi wyoming 衛星、瀏覽器衛星、記憶沙盒、ESP32 edge-mix（2026-10-08）；憂鬱指數／DNA 突變／人格排班、新聞插播（2026-10-09–10）；callback_queue（2026-10-11，由事件追問取代）；DJ revival mode 與熱聊短版（2026-10-11）。`run_bot.py` 仍設 `MARVIN_ALT_RESCUE=shadow`，但程式碼已不讀。

---

## 🐞 已知 bug / 風險

依嚴重度排序。🔴 = 開放給其他伺服器前必須處理。

| # | 問題 | 證據 | 狀態 |
|---|---|---|---|
| 🔴1 | **多伺服器同時使用會互相干擾**：單一 engine / sink / 文字頻道 / 音樂佇列、voice client 取「第一個」、最後一人離開會斷開所有伺服器 | ARCHITECTURE.md §7a | 未處理 |
| 🔴2 | **記憶沒有按伺服器隔離**：prod 只有一個 `MemoryManager()`，恆寫主伺服器分區 | `gemini_router.py:177` | 未處理 |
| 🔴3 | **身分用顯示名稱**：同意、記憶、口味都綁暱稱 | `consent_manager.py:52`、`discord_voice_engine.py:1003` | 未處理 |
| 🔴4 | `/marvin_reboot` 沒權限檢查 | `cogs/voice_controller.py` | ✅ 已修（Phase 1 Stage A，10/1 合併：限 owner、不貼 pull 輸出） |
| 🔴5 | 資料清理排程停用 | `~/Library/LaunchAgents` | ✅ 已修（10/2 起每日 03:00 維護排程） |
| 🔴6 | 語音頻道進出紀錄不看同意 | `presence_logger.py` | ✅ 已修（Phase 1 Stage A：只記 Marvin 所在頻道與已同意者） |
| 🔴7 | 沒有每伺服器配額 / 同時房間上限 | OPERATOR.md §5 | 未處理 |
| 🔴8 | 沒有「馬文忘掉這件事」／「這個別說出去」語音撤回 | TODOS.md | 未處理（事件記憶上線後更需要） |
| 🟠9 | 同意通知與 `consent_manager.py` 仍列 SambaNova（9/29 已拔），多揭露一家 | `consent_manager.py:7,23` | 未改 |
| 🟠10 | 事件記憶抽取雜訊：STT 糊句被抽成事件、對馬文的指令被當成事件 | 9/29 重播 | 拼圖制（湊齊 who+what+when）擋掉一部分，觀察中 |
| 🟠11 | STT 全 bot 一條 `Semaphore(1)`（另有喚醒專用一條） | `discord_voice_engine.py:812` | 人一多排隊延遲上升 |
| 🟠12 | DJ 口白零星編造（「店裡」）、歌名寫錯、新聞同事件不同媒體重複講 | `records/dj_narration.jsonl` 10/11 | 未處理 |
| 🟡13 | `macos_stt_bin`（v1，喚醒偵測用）沒進 git、也沒有寫下編譯指令 | DEPENDENCIES.md §3 | 換機器會缺喚醒偵測 |
| 🟡14 | `music_memory.json` 沒有檔案鎖，長跑腳本與 bot 同時寫會互蓋 | 營運筆記 | 口味資料遺失 |
| 🟡15 | 冷場話題的 LLM 失敗時，fallback 字串「我想不到好話題，等一下再試」也會被念出來 | `topic_generator.py:16,102` | 未處理 |
| 🟡16 | `run_bot.py` 註解寫記憶 callback 是「dry-run 觀察」，實際是 live | `run_bot.py:25` | 誤導 |
| 🟡17 | `RealtimeVADSink.cleanup()` 在物件未完整初始化時 `__del__` 會 `AttributeError` | pytest 警告 | 只出現在測試 |
| 🟡18 | CI 用 3.12、prod 用 3.13，`audioop` 路徑 CI 測不到 | DEPENDENCIES.md §4 | 版本相關問題 CI 抓不到 |

---

## 最近的變更（2026-09-27 之後）

| PR / Commit | 日期 | 摘要 |
|---|---|---|
| `d1e29e0` 本機 | 10-11 | DJ 拔 revival；熱聊一律不講話直接 crossfade；「Marvin推薦」不再被當成真人寫進素材 |
| `9c49c2a` 本機 | 10-11 | 事件記憶拼圖制（who+what+when 湊齊才用）＋後續關心併回既有事件 |
| #119 | 10-11 | 事件記憶 owner 改由本地依原文主詞判定，第三人與陪病事件不收 |
| #118 | 10-11 | 事件記憶 Phase 2：事件過後追問，整套取代 callback_queue |
| #117 | 10-10 | 事件記憶 Phase 1（問了能答＋學老家）＋ DJ perspective mode＋付費口白入口 |
| #116 | 10-10 | DJ 口白改純觀察者語氣、拔情緒記憶 mode、選歌理由新句型 |
| #115 | 10-09 | 拔除憂鬱指數 / DNA 突變 / 每日人格排班 |
| #113 | 10-09 | 單一 mixer 第 3b 刀（刪 satellite 進程等）＋ DJ 口白註冊表第 1–3 刀 |
| #112 | 10-09 | 藍牙發射板：上車秒連、音量不再二次衰減、BMW／Lexus 配對 |
| #111 | 10-08 | 車載 app 搬進 Discord 進程＋上車自動進頻道 |
| #110 | 10-08 | 喚醒後回應自然接上：gap 不再否決、web 問題走 grounded、播放專輯 |
| #109 | 10-08 | Discord mixer 輸出 tap＋`/audio_stream` |
| #108 | 10-06 | 原版 ESP32 藍牙發射板（BMW/Soundcore 自動切換） |
| #107 | 10-06 | 解密失敗 per-SSRC 歸因 log（純觀測） |
| #106 | 10-06 | car puck 裝置自帶身分（多人同車） |
| #105 | 10-04 | autopilot 一組三首＋懷舊 crossfade＋口白依熟悉度分流 |
| — | 10-01–02 | Phase 0/1 信任與隱私 Stage A–D（權限、進出紀錄、同意通知、資料保留清理）；日記停止貼文、舊事追問併進 DJ |
| — | 09-29–30 | 減法：拔 `/marvin_talk`、Suno、四個 shadow、主題歌單、串流 STT、companion、SambaNova；DJ 老朋友三槽素材、扭蛋化 |

整體方向：定位是「遠距朋友的虛擬客廳」裡的電台主持人。9 月底到 10 月初持續做減法（單一進程、單一 mixer、拔掉沒在用的 shadow 與人格機制），新增的是事件記憶（記得你說過要做的事、事後關心）和 DJ 口白品質。多伺服器架構（🔴1–3、7）還沒動。
