
## 新功能 — 待完成

### TODO: 語音逐字稿儲存層
**Status:** SHIPPED（2026-05-14）
**What:** 所有 voice channel 語音轉錄自動存入 SQLite，每筆含 `speaker_id`、`guild_id`、`timestamp`、`text`。
**Why:** 「壓縮時間變 prompt」的基礎資料層。沒有這個，以下所有記憶功能無從建起。
**How to start:** 在 STT 回調後加 `transcript_store.save()` call，建 `transcripts` table。
**Effort:** S（半天）

---

### TODO: Living Profile 壓縮器
**Status:** SHIPPED（2026-05-14）
**What:** 每日背景 LLM job，把過去 N 天逐字稿壓縮成 per-user semantic profile：習慣、進行中的事、決策模式、常提到的人與關係。
**Why:** 讓使用者不需要解釋背景，Marvin 已經知道。「用過去時間節省現在時間」的核心元件。
**How to start:** asyncio 定時任務（每24小時），讀 `transcripts` table，呼叫 LLM 產生 JSON profile，存 `user_profiles` table。
**Depends on:** 語音逐字稿儲存層
**Effort:** M（2天）

---

### TODO: 向量語意搜尋
**Status:** SHIPPED（2026-05-14）
**What:** 對 user profile 和逐字稿建立 embedding index，查詢時找出與當前問題最相關的過去片段（top-k semantic retrieval）。
**Why:** 不是所有過去都跟當前問題有關，向量搜尋讓 context 注入精準，不是把所有歷史塞進 prompt。
**How to start:** 用 `sqlite-vec` 或本地 `chromadb`，對每個 profile segment 建 vector。查詢時語意相關 top-k。
**Depends on:** Living Profile 壓縮器
**Effort:** M（2天）

---

### TODO: Context 自動注入
**Status:** SHIPPED（2026-05-14）
**What:** 使用者向 Marvin 提問前，系統自動從 profile 撈出相關上下文，prepend 到 LLM prompt，使用者感覺不到這個過程。
**Why:** 讓「你說『那件事』，Marvin 就知道是哪件事」真正運作的最後一哩路。
**How to start:** 在 `stream_fast_response` 之前加 `context_injector.enrich(speaker, query)` call，返回 enriched prompt。
**Depends on:** 向量語意搜尋
**Effort:** S（1天）

---

## 低重要性 — 暫不執行

### TODO: Twitch 上線通知 cog
**Status:** DEFERRED（暫不執行）
**What:** 當指定 Twitch 頻道開台，Marvin 自動發通知到指定 Discord 文字頻道。
**Why deferred:** 免費工具（Pingcord）已解決此需求，非差異化功能。需要時 1-2 天可補上。
**How to start:** Twitch EventSub `stream.online` → 新增 `cogs/twitch_notify_cog.py`。

---

### TODO: Reaction roles cog
**Status:** DEFERRED（暫不執行）
**What:** 成員對特定訊息加 emoji reaction → 自動獲得對應身份組。
**Why deferred:** Discord Onboarding 原生支援，非差異化。需要時半天可完成。
**How to start:** `on_raw_reaction_add` / `on_raw_reaction_remove` → 新增 `cogs/reaction_roles_cog.py`。

---

### TODO: STT 替換為 Deepgram
**Status:** DEFERRED（暫不執行）
**What:** 將目前 macOS-only 的 Swift/MLX Whisper STT 替換為 Deepgram API（延遲 ~150ms，$0.0043/分鐘）。
**Why deferred:** SaaS 化前置條件，但目前產品仍在本地驗證階段。
**Blocks:** 多租戶架構、雲端部署
**Effort:** M（2-3 天）

---

### TODO: Marmo webhook — text-channel fallback
**Status:** DEFERRED（暫不執行）
**What:** Marvin 不在語音頻道時，webhook 結果改送文字頻道而非靜默丟棄。
**How to start:** `MarmoServer._handle_result()` 加 voice_clients 空值判斷，改送 `active_text_channel`。
**Effort:** S（1小時）

---

### TODO: speak_via_marvin() 錯誤處理
**Status:** DEFERRED（暫不執行）
**What:** Marvin webhook 下線時，NemoClaw 側 `aiohttp.ClientError` 優雅降級而非拋出未處理例外。
**How to start:** `session.post()` 加 `try/except aiohttp.ClientError`，log warning 繼續執行。
**Effort:** XS（30分鐘）

---

### TODO: NemoClaw Smart Router 觀察期
**Status:** IN PROGRESS（低優先）
**What:** 跑一輪後分析 log 確認 auto-route 觸發率合理、無假陽性。
```
grep "NemoClaw路由\|NemoClaw→\|NemoClaw.*跳過\|NemoClaw.*排隊" bot_main.log | tail -50
```
**前置條件:** Bot 在有主人在線的 session 跑至少 30 分鐘。

---

### TODO: 方案2 音訊直輸
**Status:** DEFERRED（前置條件未完成）
**What:** 喚醒後將 `wav_bytes`（16kHz PCM）直送 Gemini Audio，跳過 STT 文字層。
**Depends on:** 喚醒誤觸率診斷完成 + NemoClaw 觀察期通過
**Effort:** M

---

### TODO: Docker / Linux path
**Status:** DEFERRED（待首個 Linux 用戶確認後再處理）
**What:** Full Docker image。Linux voice path (Whisper-only) 需 end-to-end 驗證。
**Effort:** M (~2 hours CC, 1-2 days human)

---

### TODO: 性格突變 3.0 — Phase 2
**Status:** DEFERRED（需 Phase 1 資料先跑幾場）
**What:** per-player DNA apply — `player_reactions[speaker]` overlay global DNA。
**Blocked by:** Op 33 Phase 1 需 3-5 sessions 資料。

---

### TODO: voice_controller.py refactor
**Status:** DEFERRED（需更多 git commit 積累後進行）
**What:** 拆解 4,397 行 God file → AudioPipeline / LLMOrchestrator / PersonalityEngine。
**Effort:** L（3-5 sessions）。Risk: High。

---

### TODO: 串流觀眾轉化智能
**Status:** DEFERRED（暫不執行）
**What:** Twitch EventSub per-viewer 行為追蹤，計算轉化優先分數，偵測「鑽石機會」觀眾。
**Why deferred:** 本質是 Twitch API 資料分析，與全時語音喚醒無關，任何開發者都能建。不是 Marvin 的核心差異化。
**Effort:** M（3天）

---

### TODO: 開播前簡報系統
**Status:** DEFERRED（暫不執行）
**What:** 開播前30分鐘預熱觀眾資料，前5分鐘推送重點觀眾摘要給實況主。
**Why deferred:** 依附於串流觀眾轉化智能，同樣與語音架構無關。
**Depends on:** 串流觀眾轉化智能

---

### TODO: 直播中耳語系統
**Status:** DEFERRED（暫不執行）
**What:** 事件驅動的實況主即時語音提醒，每分鐘上限一則。
**Why deferred:** 依附於串流觀眾轉化智能，同樣與語音架構無關。
**Depends on:** 串流觀眾轉化智能

---

## Twitch 整合 — 不執行（參考用）

> 產品方向從「聲音克隆 agent」轉向「上下文壓縮助理」後暫停。保留作為未來 Twitch-first 商業化路徑參考。

### TODO: ElevenLabs 聲音克隆 onboarding
**Status:** NOT EXECUTING（參考）
**What:** Twitch VOD → Demucs 人聲分離 → ElevenLabs Professional Voice Clone → Marvin 使用實況主聲音說話。
**Why parked:** Uncanny valley 風險、需要實況主預先審批流程、產品核心已轉向上下文壓縮。若未來有明確聲音克隆需求可重啟。

---

### TODO: VOD 個性訓練（說話方式克隆）
**Status:** NOT EXECUTING（參考）
**What:** 分析 VOD 字幕 + Twitch 聊天紀錄，提取用詞習慣注入 system prompt。
**Why parked:** 依附於聲音克隆方向，一起暫停。

---

### TODO: Landing page 實作
**Status:** NOT EXECUTING（參考）
**What:** 將 `DESIGN.md` 做成可部署靜態頁面。
**Why parked:** 設計稿基於「語音克隆 agent」定位，產品定位已轉向，頁面需從頭重定義再實作。DESIGN.md 保留設計系統參考價值。

---

### TODO: 統計 API endpoint
**Status:** NOT EXECUTING（參考）
**What:** `/api/stats` 回傳 `{"servers": N, "voice_hours": N}`。
**Why parked:** 依附於 landing page，一起延後。

---

## 已完成

### TODO: Approach B — Semantic emotion detection
**Status:** SHIPPED（Op 31, 2026-05-07）
**What:** `_classify_marvin_self_emotion(speaker, full_text)` 背景任務，Groq flash 分類 Marvin 自身文字情緒，結果存 `marvin_self_emotion` 供下次 TTS prosody 使用。
