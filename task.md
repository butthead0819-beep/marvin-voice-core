# Marvin 穩定性與語言一致性任務清單

## 1. Operation Memory Shield (背景效能隔離)
- [x] 在 `gemini_router.py` 實作 `allow_local=False` 機制
- [x] 禁止 5 分鐘背景任務呼叫 local M1 GPU 推論
- [x] 在 `voice_controller.py` 的 `slow_system_loop` 加入 `try-except` 保護
- [x] 驗證即便背景任務失敗，主程式不會崩潰

## 2. Operation Language Guard (語言一致性加固)
- [x] 精簡 `marvin_prompts.py` 中的 `ambient_diary` 指令（縮減 30% 並改為條列式）
- [x] 在 `get_instruction` 注入全域繁體中文強勢指令
- [x] 驗證 Tier-2 (Qwen 4b) 在背景執行時輸出為繁體中文
- [x] 監控日誌確認語言飄移問題已解決

## 3. 系統狀態監控
- [x] 確認 Tier-1 (Gemini) 熔斷恢復機制正常 (已透過自動重試驗證)

## 4. Operation Priority Reorder (LLM 呼叫優先序重排)
**背景**：`gemma-4-31b-it` 頻繁 503 UNAVAILABLE，導致登場問候語延遲 ~2 分鐘。
改為最穩定的提供者優先，Gemini 退為最後雲端選項。

新優先順序：**Groq → Cerebras → Gemini → Ollama Tier-2/3**

- [x] `_call_llm`：改寫為 Groq → Cerebras → Gemini → `_dispatch_fallback_chain`
- [x] `stream_llm`：改寫為 Groq → Cerebras → Gemini → Ollama Tier-2/3
- [x] `_dispatch_fallback_chain`：移除重複的 Groq/Cerebras 區塊（已在上層處理）

## 5. Operation No-Hang (Gemini API 掛起防護)
**背景**：`_call_cloud` / `_stream_cloud` 呼叫 Gemini 時無 timeout，
過載時 HTTP 連線掛住 ~40s × 3 retries ≈ 2 分鐘。

- [x] `_call_cloud`：加入 `asyncio.wait_for(..., timeout=8.0)` 包住 `generate_content`
- [x] `_stream_cloud`：加入 `asyncio.wait_for(..., timeout=8.0)` 包住 `generate_content_stream`

**效果**：最壞情況 8s × 3 + Groq 回應 ~2s = ~26s（原本 ~120s）

## 6. Operation Slow Loop 功能啟用
- [x] ~~`batch_extract_memories`：在 `slow_system_loop` 啟用~~ → **已移除**（見 Op 12）
- [x] `analyze_social_dynamics` + gap filling：在 `slow_system_loop` 啟用（同上保護）
- [x] 確認最大 LLM 負載 ~1.7 calls/min，遠低於 Groq 30 RPM 上限

## 7. Operation Silent Cloud (減少聊天室打擾)
**背景**：`_trigger_fallback_notification` 在每次 `_call_llm`/`stream_llm` 都觸發（因為
`"Tier-1 (Groq)" != "Tier-1"`），導致每次 LLM 呼叫發 2 條 Discord 訊息。

- [x] `_call_llm`：移除 Groq/Cerebras/Gemini 三處的 `_trigger_fallback_notification` 呼叫
- [x] `stream_llm`：移除 Groq/Cerebras 兩處的 `_trigger_fallback_notification`，改在成功後呼叫 `_reset_tier_to_primary()`（確保 Tier-2/3 恢復後有通知）
- [x] `handle_fallback_notification`：只處理 Tier-2/3 降級與恢復，其他層級變化一律忽略

**效果**：聊天室訊息只在真正降到 Ollama 時才出現，正常運作完全靜音。

## 8. Operation Richer Emotion (情緒深化)
**背景**：Token 預算空間充裕，但多個影響情緒的系統是死代碼或從未啟動。

### 修復的死代碼
- [x] `interaction_count` 從未自動累加 → 在 `stream_fast_response` 成功後呼叫 `increment_stat(speaker, 'interaction_count', 1)`，讓關係階段（陌生人→熟人→老友→摯友）能真正隨時間推進
- [x] `dna.randomness=8` 欄位從未注入 → 加入「思路跳躍性」描述（≥7 = 意外聯想，≤3 = 疲憊線性）

### 新增情境感知
- [x] 時段感知：依當前小時注入「深夜/清晨/午後/夜間」語氣語境，影響所有 dna_sensitive 層的 prompt
- [x] 今日疲勞感：`_session_call_count` 在 router 記錄每次回應，≥5 次注入被呼喚次數，≥20 次加入「大腦已嚴重超載」修飾

### 已知剩餘缺口（未修復，需另行設計）
- `highlight_of_the_day` 欄位：dead code，`emotional_highlights` 已透過 `get_rich_context` 正常注入，可忽略
- `pos_feedback/neg_feedback`：顯示於 UI 但無觸發來源，需定義什麼情境算正/負向回饋
- `behavioral_patterns`：欄位存在但 `update_behavioral_pattern()` 從未被呼叫

## 9. Operation Clean Diary (日記重複/無意義問題修復)
**背景**：昨晚 23:00-01:00 的 5 分鐘日記幾乎完全相同，原因：
1. `last_slow_summary` 把整段重複摘要餵回去當前情提要 → LLM 複製模板
2. 馬文的 TTS 回應被加進 conv_buffer → diary 輸入出現 "Marvin: 表達疲憊" → 被照抄進摘要
3. Groq TPD 在 01:11 耗盡（100k tokens/day），退到 Cerebras 8B → 品質下降

**修復：**
- [x] `slow_system_loop`：在傳入 `generate_slow_summary` 前，過濾掉 `speaker == "Marvin"` 的 entry
- [x] `generate_slow_summary`：`last_slow_summary` 改為只儲存/傳入第一行（話題），不再傳整段摘要
- [x] `generate_slow_summary`：加入 SKIP 指令 — LLM 判斷內容無新意時回傳 "SKIP"，函式回傳 `None`
- [x] `slow_system_loop`：收到 `None` 時跳過發文，只寫 log
- [x] `generate_slow_summary`：日記改走 Groq → Gemini 路徑（跳過 Cerebras 8B，品質不足）
- [x] `ambient_diary` prompt：加入禁止語（禁止「無關緊要」「描述無意義」等空泛短語），要求具體內容

## 10. Operation Speech Interrupt (使用者說話時中斷 TTS)
**背景**：TTS 播放中若使用者開口，語音會繼續播放直到播完，造成使用者感覺被忽視。

- [x] `play_tts`：新增 `already_in_channel: bool = False` 參數
- [x] `play_tts`：在 `playback_lock` 內、`voice_client.play()` 前設定 `self._current_tts_text` 與 `self._current_tts_in_channel`
- [x] `play_tts`：在 `finally` 清空 `self._current_tts_text = ""`
- [x] `handle_raw_speech_start`：偵測到 `is_playing_audio` 時呼叫 `vc.stop()`，若文字未發到聊天室則補發 `💬 【馬文·被打斷】`
- [x] 所有已有 Discord 訊息的 `play_tts` 呼叫加上 `already_in_channel=True`（點名/送客/額度警告/降臨/快系統/社交補位/主動發言/指令/頻率共鳴）
- [x] 無配對 Discord 訊息的呼叫（嘲諷/等待提示/音樂失敗）保持 `already_in_channel=False`，被打斷時自動補發

**效果**：使用者說話時馬文立即停嘴；未發過文字的 TTS（嘲諷、等待語）被打斷時也會留下文字記錄。

## 11. Operation False Wake (誤喚醒修復)
**背景**：17:47-17:53 發生 4 次無喚醒詞的誤喚醒，根因確認為 `stt_cleaner` LLM 過矯正。

**根因**：
- `stt_cleaner` prompt 有「若全文中出現 `文...` 等音近詞彙，務必修正為馬文」的 catch-all
- LLM (Groq llama-3.1-8b-instant) 將「**寫完**的」「**白寫**」等含 wen/wan 音的普通字詞過度矯正成含「馬文」的文字
- 清洗後的文字含「馬文」→ `pre_filter_speech` 回傳 "force_intervene" → `is_fast=True` → 誤喚醒

**修復：**
- [x] `marvin_prompts.py` `stt_cleaner`：移除開放 catch-all「文...」，改為「僅在句子前兩字出現特定詞彙時才修正，禁止修正句中字詞」
- [x] `gemini_router.py` `clean_stt_text` `_build_res`：加入「Wake Injection Guard」— 若原始文字不含喚醒詞但 LLM 輸出含喚醒詞，拒絕此次清洗結果並記錄 WARNING

**效果**：雙重防禦。第一層改善 LLM 行為；第二層確保即使 LLM 仍過矯正，喚醒詞注入也會被截斷並記錄於日誌。

## 12. Operation API Economy (移除逐段記憶萃取)
**背景**：使用者改用 web LLM 每日整體記憶萃取，比逐段更有品質，節省 API 用量。

- [x] `slow_system_loop`：移除 `batch_extract_memories`，`asyncio.gather` 從 3 任務改為 2 任務
- [x] 修正 `results[1]` 索引對應 `analyze_social_dynamics` 結果

## 13. Operation System Oracle (語音查詢系統狀態)
**背景**：喚醒後能直接用語音詢問 API 用量、系統健康狀態。

- [x] `/marvin_system`：加入 Token 預算進度條（`SukiBudget.get_info()`）與動態 embed 顏色（正常→黃→橘→紅）
- [x] `_handle_voice_status_query()`：新方法，語音觸發系統健康報告，零 LLM 開銷
- [x] `_process_queued_query`：加入 `_status_keywords` 早退機制（「系統狀態」「剩餘額度」「token 剩」等 12 個觸發詞）
- [x] **Bug Fix**：`get_status()` → `get_info()`（`SukiBudget` 無 `get_status` 方法，導致 `/marvin_system` 掛起 5 分鐘）

## 14. Operation Wide Search (擴展網路搜尋觸發詞)
**背景**：原觸發詞過少，大量時效性問題無法觸發 DuckDuckGo 搜尋。

- [x] `gemini_router_llm.py` `_should_local_search`：觸發詞從 14 個擴展到 ~30 個
- [x] 加入英文觸發詞（`what is`, `who is`, `how to` 等）
- [x] 查詢長度限制從 50 字擴展到 80 字
- [x] 加入 stop_words 過濾（「你」「馬文」「幫我」等不計入長度）

## 15. Operation Confidence Gate (回應信心度門檻)
**背景**：喚醒後若只叫名字或語意不明，馬文會嘗試強行回應，品質低落。

- [x] `marvin_prompts.py` `fast_awakening`：加入 `[SKIP]` 信心度門檻規則
  - 純叫名字（「馬文」「hi馬文」）或空白 → 輸出唯一一行 `[SKIP]`
  - 完全無法判斷意圖 → 輸出 `[SKIP]`
  - 禁止「不知道」「不確定」「無法回答」等軟弱回應
- [x] `_process_queued_query`：偵測到 `[SKIP]` 時不播 TTS，改在文字頻道發出 `💬 【馬文·聽不懂】` + 馬文式嗆聲回應
- [x] 加入 `_WEAK_PATTERNS` 過濾器（「不知道」等詞彙→強制替換為嗆聲台詞）

## 16. Operation Self-Improvement Loop (自我改善回饋迴圈)
**背景**：馬文的回應品質無法自動追蹤，需要可觀測的數據支援每日改善。

- [x] `_schedule_reaction_check(speaker, bot_response, respond_time)`：回應後等待 20 秒，收集玩家接下來 ≤3 句話
- [x] `_classify_and_log_reaction()`：用 LLM 分類玩家反應（嚴重/錯誤/提出興趣/喜歡），寫入 `records/response_feedback.jsonl`
  - `嚴重`：20 秒內無任何反應
  - `錯誤`：玩家無視或更正馬文
  - `提出興趣`：玩家追問
  - `喜歡`：玩家笑/稱讚/繼續話題
- [x] 每次 `stream_fast_response` 成功後觸發 `asyncio.create_task(_schedule_reaction_check(...))`
- [x] Record 格式：`{timestamp, speaker, bot_response, reaction_type, reason, raw_reaction}`

## 17. Operation Daily Snapshot (每日 Log 快照)
**背景**：web LLM 無法自行切割 stt_history.log 的時間區間，需要預先切好的每日檔案。

- [x] `daily_log_export_loop`：每天 UTC+8 12:00 自動執行
- [x] 輸出路徑：`records/daily/YYYY-MM-DD.log`（以到期日命名）
- [x] 涵蓋：前一日 12:00 ～ 當日 12:00 的 STT 紀錄 + response_feedback.jsonl 紀錄
- [x] 格式：兩段式（`=== STT LOG ===` + `=== RESPONSE FEEDBACK ===`），直接可貼入 web LLM

## ✅ 已部署 (2026-04-26)
所有 Op 1–17 變更皆已上線。

### Op 18. 語音頻道斷線修復
- [x] `summon` 指令的 `except Exception` 補上 2 秒等待 + 重試 `channel.connect()` 邏輯，防止 DAVE CryptoError flood + soft_repair 競爭導致 Marvin 無聲消失

### Op 19. Clyde 情緒貼圖系統
- [x] 新增 `sticker_manager.py`：`StickerManager` + `infer_mood()` + 25 秒冷卻
- [x] `main_discord.py`：啟動時載入 Clyde 貼圖包
- [x] `voice_controller.py`：`_send_mood_sticker()` 接入快速回應、玩家進入、玩家離開三個觸發點

## ✅ 已部署 (2026-04-28)

### Op 20. 台式冷笑話 Few-Shot Examples
**背景**：用戶貼上 26 個台灣冷笑話作為風格範例，讓馬文學習創作而非照抄。

- [x] `marvin_prompts.py` `joke` key：加入 10 個 few-shot 範例（諧音梗、小明系列、動物梗等）
- [x] 明確禁止照抄，要求學習風格後全新創作
- [x] 列出 6 大笑話類型供 LLM 擇一或混搭
- [x] 要求笑完後以馬文口吻嘆息，將冷場感與「宇宙徒勞」連結

### Op 21. YouTube 串流播放系統
**背景**：用戶要求支援非本地音樂串流，使用 yt-dlp 解析 YouTube/SoundCloud。

- [x] `_resolve_yt_query(query)`：yt-dlp 在 `run_in_executor` 非同步解析，返回 `{title, uploader, url, thumbnail, webpage_url, duration}`
- [x] `_stream_loop()`：依序播放佇列，push history，播完發送佇列空訊息
- [x] `play_stream_song(url, title)`：取 `playback_lock`，建立 `PCMVolumeTransformer`，等待 done event
- [x] `stop_stream(reason)`：取消任務、停止 vc、重置所有串流狀態
- [x] `/marvin_play <query>`：加入佇列並啟動 `_stream_loop`
- [x] `/marvin_skip`：`vc.stop_playing()` 跳下一首
- [x] `/marvin_queue`：純文字列出佇列
- [x] `/marvin_play_queue`：互動 embed + `QueueControlView`（Select 選歌 + 跳到/刪除按鈕）
- [x] 新增 `stream_history` 列表供前一首功能使用
- [x] `play_tts` 修改：TTS 前若 `stream_mode` 啟動則先呼叫 `stop_stream`

### Op 22. 移除 Ducking + /marvin_play_control
**背景**：ducking fade 影響音樂體驗，完全移除；新增互動控制面板。

- [x] 移除 `_radio_fade_task` 啟動（`start_radio`、`marvin_play` 皆不再啟動）
- [x] `play_radio_song` / `play_stream_song`：固定使用 `self.radio_volume`，移除條件式 `0.01` 起始音量
- [x] 移除 `/marvin_play_vol` 指令與 `stream_vol_locked` 旗標
- [x] 新增 `PlayControlView`（`discord.ui.View`，timeout=3600）：
  - Row 0：⏮️ 上一首（history pop + 重排佇列）、⏸️/▶️ 暫停/播放（`vc.pause/resume`）、⏭️ 下一首（`vc.stop_playing`）
  - Row 1：🔉 -5%、🔊 +5%（更新 `_radio_source.volume` + `radio_volume`）
  - `_build_embed()`：顯示當前歌曲、音量、狀態、佇列/history 數量
- [x] `/marvin_play_control`：發送帶 embed 的控制面板

### Op 23. 串流 LLM 評語
**背景**：與 marvin_radio 一致，讓馬文對 YouTube 歌名產生憂鬱/嘲諷短評。

- [x] `gemini_router_content.py` `generate_dynamic_system_msg`：新增 `"stream_now_playing"` prompt key（針對 YouTube 歌名與頻道名，嘆息有人特地去找歌）
- [x] `_stream_loop`：embed 先以 `「...」` 送出（不阻塞播放），`asyncio.create_task(_update_stream_comment(...))` 背景生成評語後 edit embed
- [x] 模式與 `_update_radio_comment` 完全一致：保留縮圖、時長、連結欄位

---

## [2026-05-01] 修復與強化

### Op 19 修復：Clyde 情緒貼圖系統
**問題**：StandardSticker（fetch_sticker_packs 回傳）無法透過 `channel.send()` 發送。

- [x] `sticker_manager.py`：捨棄 `fetch_sticker_packs()`，改從 `guild.stickers` 載入 `GuildSticker`
- [x] 新增 `_emoji_mode`：Guild 無 Sticker 時降級為情緒 emoji（`😒😤😞✨😊🤔...`）
- [x] `USE_EXTERNAL_STICKERS` 不足時也自動降級，並在下次嘗試時繼續用 emoji

### Op 21 音量調整
- [x] `stream_volume` 初始值 `0.50` → `0.10`（與 radio_volume 一致，初始 10%）
- [x] `PlayControlView.VOL_STEP` `0.15` → `0.05`（步進 5%）
- [x] 按鈕標籤更新：`🔉 -15%` → `🔉 -5%`，`🔊 +15%` → `🔊 +5%`

### Suno 歌詞量強化
- [x] `marvin_prompts.py` `songwriter_director` `lyrics` 欄位：要求 `[Verse 1]+[Chorus]+[Verse 2]+[Chorus]`，至少 20 行，建議 30-50 行
- [x] `gemini_router_content.py` `generate_song_blueprint()` user_prompt 末尾加入歌詞量強制要求

### STT Cleaner 對映加強
- [x] `marvin_prompts.py` `stt_cleaner`：新增 `罵文`、`雅文`（句首）為必修正對象
- [x] 加入特別警示，標注最常見 STT 誤判（罵文/媽問/艾瑪文/雅文）

### fast_awakening 改善
- [x] `<think>` 從 4 行縮為 **2 行**，推理步驟簡化（去掉情緒解讀/真實意圖）
- [x] 新增遊戲/下載進行中回應 **嚴格 20 字限制**

### suki_memory.json 更新（2026-05-01 LLM 清洗）
- [x] showay：學歷「電機相關」、tech_stack 加 Ollama、impression 更新為本機算力主題
- [x] 狗與鹿：taboos 新增火柴禁忌、hobbies 加 AI音樂、硬體加 Insta360
- [x] weakgogo：職業「工程」、hobbies 加佛學研究/投資、impression 全面改寫
- [x] proactive_topics：替換為「STT幻覺大戰」、「魔物獵人中年危機」
- [x] marvin_performance / wake_analysis / system_suggestions / _meta 更新為 2026-05-01 資料

---

## ✅ 已部署 (2026-05-07)

### Op 24. Cron 環境修復 (launchd EINTR + Operation Not Permitted)
**背景**：每日 12:05 的 `suki_memory.json` 更新從未成功執行。
`com.antigravity.marvin.dailyreview` exit 78，log 顯示 Python 在 `frozen getpath` 收到 `EINTR (errno 4)` 崩潰；
`com.antigravity.marvin.dailyslice` 直接呼叫 Python，無法取得檔案存取權限（Operation Not Permitted）。

- [x] `com.antigravity.marvin.dailyreview.plist`：加入 `EnvironmentVariables`（`HOME`、`PATH`、`PYTHONNOUSERSITE`、`PYTHONDONTWRITEBYTECODE`）
- [x] `com.antigravity.marvin.dailyslice.plist`：改為透過 bash wrapper 執行（原本直呼 Python，無重試機制）；同樣注入 `EnvironmentVariables`
- [x] `scripts/run_daily_review.sh`：新增 `export` 環境變數、每次 attempt 加時間戳 log、retry 間隔從 10s 延長至 15s
- [x] `scripts/run_daily_slice.sh`：**新建**，與 `run_daily_review.sh` 架構一致
- [x] 重新 reload LaunchAgent，`launchctl list` 確認 exit code 由 78 回到 0

### Op 25. 回應模板多樣化 ([SKIP] Confidence Gate 改善)
**背景**：4 個 `_WEAK_REPLACEMENTS` 全以「問題太模糊」框架呈現，對閒聊觸發場景完全不適用；且 4 句輪換太快，玩家明顯感受到重複。

- [x] `cogs/voice_controller.py` `_WEAK_REPLACEMENTS`：從 4 句擴展為 8 句，移除所有以「問題」為框架的句子
- [x] 新句型涵蓋三種情境：叫名字後沒下文、語音辨識殘缺句、閒聊意外觸發喚醒詞

### Op 26. 投機預取正式接線 (Phase 3 Speculative Prefetch)
**背景**：`_pending_prefetch` 字典在 `GeminiRouter.__init__` 初始化，`_process_queued_query` 也有讀取邏輯，但從未有任何地方填入資料，導致 Speculative Cache 永遠 MISS，喚醒延遲（最高 19.9s）得不到改善。

- [x] `cogs/voice_controller.py` `handle_stt_result`：在 `query_queue.put()` 之後，若剝除喚醒詞後的查詢 ≥ 6 字，立即啟動 `asyncio.create_task(_speculative_response(...))` 並存入 `_pending_prefetch[speaker]`
- [x] 新任務啟動前先取消同一玩家舊有的未完成預取
- [x] `_process_queued_query` 現有的 Cache HIT 讀取邏輯無需更改，已自動受益
- **預期效果**：含問句內容的喚醒（佔多數情況），LLM 在 `_confirmation_flow` 等待期間已預熱完成，回應延遲可縮短 3-8s

### Op 27. 環境陳述句過濾 (Ambient Statement Gate)
**背景**：`get_harvest(before=3.0, after=1.0)` 抓取時間窗口內所有發言人的語句，導致玩家對他人說的陳述句（「我告訴你, 所有人都不懂」「我在回家」）被當成 Marvin 的問題，觸發錯誤回應（評分嚴重）。

- [x] `cogs/voice_controller.py` `_query_quality_gate`：新增 `ambient_statement` 過濾層
- [x] 若 query 不含任何疑問詞或指令詞，且匹配典型「對他人說話」模式（「我告訴你」「所有人都」「我在回」「我去」「大家」「再見」等 11 個 pattern），直接返回 `False, "ambient_statement"`

### Op 28. suki_memory 延伸涵蓋 (analyze_daily_log coverage gap 修復)
**背景**：`analyze_daily_log.py` 每日 12:05 執行，只讀取 slice 窗口（昨日 12:00～今日 12:00）的 feedback。12:00 之後的所有 feedback 資料要等到隔天才被納入，造成一天的學習延遲。

- [x] `scripts/analyze_daily_log.py`：在載入切片 feedback 後，額外呼叫 `load_feedback_for_window(end_dt, now)` 補抓今日 12:00 以後的資料
- [x] 若有補入資料，log 印出延伸涵蓋範圍與筆數
- [x] user_content prompt 標注實際涵蓋至的時間，讓 Gemini 能正確寫入 `_meta.log_range_end`
