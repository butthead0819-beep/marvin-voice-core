# Agent Memory — Marvin Discord Voice Bot

> **給接手的 coding agent**：這是從 Claude Code 的 per-project 記憶導出的累積知識（44 條，截至 2026-06-06）。每條是過去 session 學到、無法從 code/git 直接看出的事實、修正、決策或踩雷。**讀 code 前先讀這份**，能省下重新踩坑的時間。
> 搭配 `CLAUDE.md`（硬性工作守則）+ `AGENTS.md`（入口）一起看。
> 注意：條目含日期；citation 的 file:line 可能已漂移，引用前先對現有 code 驗證。

## 目錄

### 🧭 Feedback — 工作守則 / 修正（含 why）
- [daily_feedback_ritual](#daily-feedback-ritual) — 每個新對話 session 開始時主動讀昨天的 feedback 報告 + pipeline 健康，把 T3 audit 黑洞 / 沉默故障接起來
- [design_disciplines_for_future_consumers](#design-disciplines-for-future-consumers) — 每條流程都會被別的 agent 取用、收 token fee — 兩條紀律保住擴展空間
- [feedback_audit_data_purity](#feedback-audit-data-purity) — 任何「≥N 筆達標」訊號要先驗質再信；測試污染 / fixture 重複 / 假觸發會讓指標看起來有進展實際沒有
- [feedback_daily_probe_skill_decision](#feedback-daily-probe-skill-decision) — check_plan_triggers.py 的 daily probe pattern 已成熟但暫不抽 skill / library；記何時該重新討論
- [feedback_data_driven_diagnosis](#feedback-data-driven-diagnosis) — 調查延遲、卡頓、故障時，先撈原始數據定位，禁止用「可能是 X」推測當結論
- [feedback_env_gated_shadow_verify](#feedback-env-gated-shadow-verify) — shadow/feature flag 接在 env var 後面時，wire code ≠ 啟用；0 數據可能是功能根本沒開不是沒流量
- [feedback_intent_gap_threshold](#feedback-intent-gap-threshold) — agent gap 升級為「該寫 agent」的累計次數門檻，使用者偏好激進補 agent
- [feedback_land_restart_no_ask](#feedback-land-restart-no-ask) — 使用者請求 land PR / 重啟 bot 時，CI 綠就直接做，不要每次問「要不要 land / 要不要再重啟」
- [feedback_llm_bus_model_staleness](#feedback-llm-bus-model-staleness) — Cerebras/Groq/OpenRouter 等 free tier 的 model ID 會 deprecate；bus 內 hardcode 一段時間後會悄悄全 404
- [feedback_llm_calls_must_use_bus](#feedback-llm-calls-must-use-bus) — 需要呼叫 LLM 時一律走 llm_pool（bus），禁止 caller 自開 client / 寫死 model ID
- [feedback_mock_dont_self_fixture](#feedback-mock-dont-self-fixture) — 驗 LLM 或外部系統表現時，不能自己手寫 sample 當 baseline——測的是自己的能力不是產品的能力
- [feedback_trigger_excludes_sentinels](#feedback-trigger-excludes-sentinels) — 計算「累積 ≥N 筆觸發 next phase」這類門檻時，必須排除 sentinel state（UNKNOWN / 0.0 / no_match 等合法 negative）
- [skip_signal_attribution](#skip-signal-attribution) — 收到 skip 訊號時調整推薦邏輯，不是把歌標 blacklist

### 🗂️ Project — 進行中的工作、目標、決策
- [audio_per_song_loudnorm](#audio-per-song-loudnorm) — 「音量大小不穩定」抱怨時——Plan 12 音樂層怎麼做 per-song loudness 正規化、為何不用動態 loudnorm
- [bot_run_topology](#bot-run-topology) — launchd → wrapper → main_discord.py 啟動鏈、wrapper 跟 venv 的位置、cwd 必須正確
- [ci_red_2026-06-03](#ci-red-2026-06-03) — CI 連紅 13 failed 全修綠（commits 1c02c3a + c3b1635，CI run 26891033996 三 job 全 success）
- [cleaner_latency_and_response_failrate](#cleaner-latency-and-response-failrate) — 「Marvin 遲鈍」抱怨或查 latency 時——cleaner 慢的根因+已加的預算控管，以及還沒解的回應 LLM 64% 成功率
- [cryptoerror_storm_sentinel_blindspot](#cryptoerror-storm-sentinel-blindspot) — 改 Discord 頻道 bitrate → 重連金鑰沒同步 → CryptoError 風暴 → STT 糊；Sentinel 30s 寬限期盲點害自癒不觸發
- [iba_t0_wakeless_music](#iba-t0-wakeless-music) — 「點歌沒反應/放錯歌」排查時先確認 IBA-T0 wakeless 路徑有沒有 fire、query 是不是被 STT 糊掉
- [j1_improvement_loop](#j1-improvement-loop) — regex 本身不自學，但決策能力可透過三條工程化迴圈隨 outcome 資料優化
- [judge_followup_actions_2026-05-27](#judge-followup-actions-2026-05-27) — 49 條樣本後 5 條修正執行狀態 + 6/1 重收數據驗證標準
- [judge_outcomes_analysis_followup](#judge-outcomes-analysis-followup) — shadow race 上線後三天回來跑離線分析，看 J1 hit rate / latency / fallback rate
- [llm_paid_pool_wrong_key_bug](#llm-paid-pool-wrong-key-bug) — dailyreview 報 Gemini monthly spending cap 但 paid key 明明有額度時，先查 build_paid_review_pool 的 key 優先序
- [project_devlog_content_roadmap](#project-devlog-content-roadmap) — build-in-public 內容策略——Q&A 格式、雙軌(X英文技術/Threads中文故事)、發文頻率、題庫
- [project_gap_research_wedge](#project-gap-research-wedge) — 免喚醒資訊真空偵測+靜默交付的進度、領域不匹配發現、Phase 2 gating 條件
- [project_history_simon_suki_marvin](#project-history-simon-suki-marvin) — Marvin 真實起源與功能時間線（git 看不出來，git floor 是開源日 2026-05-07 騙人）
- [project_infinite_autopilot_tiers](#project-infinite-autopilot-tiers) — autopilot 佇列空補位的擴充策略——T1 團體記憶 / T2 發現(待做) / T3 回收(已上)，skip 鐵則
- [project_intent_gap_phase_a5_clustering](#project-intent-gap-phase-a5-clustering) — Daily ritual 跑的 LLM batch clustering，把孤兒 intent_type 字串合併成 cluster；門檻 2 次升級
- [project_intent_gap_pipeline](#project-intent-gap-pipeline) — 2026-05-27 上線的 agent gap 偵測 + 模板 ack 流水線；Marvin 之前的 cheap classifier
- [project_intent_rescue_pipeline](#project-intent-rescue-pipeline) — bus no-winner 時 LLM 改寫重投 + pragmatic signal 訊號回饋；env-gated 預設 OFF，shadow 預設 ON
- [project_judge_race_volume_2026-05-28](#project-judge-race-volume-2026-05-28) — Race coordinator 5/24 上線後 5 天樣本量遠低於 Plan 8 trigger「每天 ≥30」門檻，可能要重審
- [project_llm_pool_attribution](#project-llm-pool-attribution) — "LLM 池歸因/分流三部曲（#1 purpose"
- [project_plan12_local_mixing](#project-plan12-local-mixing) — 決定把串流播放核心改成本地 f32 混音（取代 hotswap second-stream）的方向、測試策略，以及 Marmo 的定位
- [project_plan_b_public_bot](#project-plan-b-public-bot) — Plan B（公開可邀請 bot）計劃已寫、冷凍待命；啟動 gate + Gemini 每 guild 成本
- [project_relaxed_zdr_tiered_retention](#project-relaxed-zdr-tiered-retention) — Marvin 隱私資料保留的方向決策——選分層保留而非硬 ZDR/fork；含 golden 蒸餾資料現況
- [project_spontaneous_manzai](#project-spontaneous-manzai) — 自發漫才（不依賴 openclaw）+ 打岔疊播 mixer 雙層；觸發條件與下一步
- [runtime_state_files](#runtime-state-files) — Bot 啟動時讀的本地 state files 清單，遷移／clone 時要從舊環境複製過來，否則 bot 看起來會像「死」但其實只是 init 後狀態歸零
- [speakbus_and_survival](#speakbus-and-survival) — Marvin 主動發話用 bid 架構（SpeakBus），以及 agent 自調/求生能力的分級路線圖與陷阱
- [speculative_stt_pipeline](#speculative-stt-pipeline) — bus 入口前用 J1 Regex / J2 Groq-8B / J3 Cleaner 三路 judges race，最快達信心門檻者勝出
- [stt_corrections_cache_and_pipeline_completeness](#stt-corrections-cache-and-pipeline-completeness) — 想優化 cleaner/wake 效率或做「per-user 口音學習/三專家投票」前先讀——資料說邊際太小，真價值在修 corrections 快取兩個 bug
- [stt_diagnostic_signals](#stt-diagnostic-signals) — 抱怨「STT 沒反應」時要按什麼順序看哪幾個 log、區分 5 種失敗模式
- [triadic_expert_pattern_domain_and_timing](#triadic-expert-pattern-domain-and-timing) — 三專家(positive/negative/biased)投票模式何時 work——用在離散穩定 token 的域 + 把 biased expert 拆去離線 curate；wake 系統是活證明
- [voice_pipeline_dave_to_stt](#voice-pipeline-dave-to-stt) — STT 核心服務的解密依賴鏈，Discord 啟用 DAVE 後 voice_recv 解外層 SRTP、davey 解內層 E2EE，斷一層 STT 全死

### 🔖 Reference — 外部資源 / 評估結論
- [reference_open_llm_vtuber_parts](#reference-open-llm-vtuber-parts) — Open-LLM-VTuber 評估結論——對 Marvin 只剩 2 個可搬零件，其餘不值得


---

# 🧭 Feedback — 工作守則 / 修正（含 why）

## daily_feedback_ritual
*每個新對話 session 開始時主動讀昨天的 feedback 報告 + pipeline 健康，把 T3 audit 黑洞 / 沉默故障接起來*

每次新的 work session 開始時（user 提任何 Discord-voice-bot 相關任務的第一句），主動讀並摘要這四份資料（昨日日期）+ 跑 **🚨 Pipeline 健康檢查**：

1. `records/feedback_analysis_<yesterday>.md` — sentiment 分布 + per-rec 明細
2. `records/audit_<yesterday>.md` — 低 confidence / 異常情境的 audit 行
3. `records/marvin_status_dashboard.html` — 計劃 + Marvin 流程 status 總覽（**只報 mtime + 給連結，不每天 open browser**）
   - 用 `stat -f "%Sm" -t "%Y-%m-%d" records/marvin_status_dashboard.html` 看最後更新日
   - 距今 ≤7 天 → 一句話帶過「📊 dashboard last refresh: YYYY-MM-DD (N days ago)」
   - 距今 >7 天 → ⚠️ 主動提醒 user：「dashboard 過期 N 天，建議我重新掃 plans + memory 後 refresh HTML（更新 records/marvin_status_dashboard.html 內容 — 重生流程圖節點狀態、計劃分類）」
   - User 同意 refresh → 重跑兩個 explore agent（plans + Marvin flow）→ 更新 HTML 既有檔（不新建）
4. **🚑 `records/rescue_outcomes.jsonl` — LLM Rescue Pipeline 校準進度（Plan 10）**
   - 跑 `python scripts/analyze_rescue_outcomes.py` 拿 by_gap_class + cluster + samples
   - 一句話報「🚑 rescue: total=N (shadow=X / convergent=Y / divergent=Z / unmatched=W)」
   - **shadow 上線階段**（MARVIN_INTENT_RESCUE_SHADOW 未設或 =1）：
     - total ≥30 + 人工抽樣 shadow_samples 改寫正確率 ≥80% → 提醒 user「夠了，可以考慮關 shadow（設 MARVIN_INTENT_RESCUE_SHADOW=0 後重啟 bot）」
     - total < 30 → 報告當前累積，預估還要多久（按過去 N 天累積速度）
     - 改寫常常離譜 → 列 ≤3 個典型錯誤句子 + 建議調 `intent_agents/rescue_classifier.py::_SYSTEM_PROMPT`
   - **正式上線階段**（SHADOW=0）：
     - convergent_clusters 有任一 cluster `ready_to_propose=true` → 提醒 user「Plan 10 Stage 3：可以提案擴 regex pattern，列出 cluster 樣本」
     - divergent ≥10 筆且 negative on current_song 占主導 → 提醒 user「Plan 10 Stage 3：可以開始做 music_agent_v2 handler 內的 pragmatic_signal 消化」
   - 檔案不存在（bot 重啟後還沒觸發任何 rescue）→ 一句話帶過「🚑 rescue: no data yet」即可
5. **🕒 `records/latency_breakdown_<yesterday>.md` — voice pipeline 延遲組成（3am batch 產生）**
   - 由 `scripts/analyze_latency_breakdown.py` 拼 STAGE_TIMING（前半）+ TTS_TIMING（首音）+ llm_routing.jsonl（回應 LLM）
   - 一句話報「🕒 latency: 最大段=X (p50≈Yms)，turns=N」
   - **STAGE_TIMING/TTS_TIMING turns=0 → 「沒人對話，無延遲數據」**（不捏造）
   - 目的：定位 baseline 2-3s 遲鈍感卡哪段。背景：LLM 中位數 ~507ms 非瓶頸，round-2 speculative pre-gen 已擱置（ROI 差），先量清楚再決定要不要動

---

## 🚨 Pipeline 健康檢查（每天 ritual 必跑，不可省略）

**Why:** 2026-06-01 incident — `analyze_daily_log.py` 從 5/24 起 merge_player 對 weakgogo 的 `emotional_highlights` 撞 str schema bug 連續炸 8 天，suki_memory `_meta.review_date` 卡在 5/23，proactive_topics 8 天沒更新。User 體感「Marvin 沒新話題」才發現。dailyreview 只發過一次 macOS 通知（容易錯過），沒任何 daily ritual 訊號暴露這個 silent failure。

User 明確要求：**有錯誤導致更新失敗，最少要在 ritual 報告中通知 user 確認**（理想是主動修復，但通知是最低要求）。

### 檢查項（每日 ritual 跑完上面四份資料後加碼跑這段）

| 訊號 | 來源 | 異常門檻 | 報告動作 |
|------|------|---------|---------|
| 🧠 suki_memory 過期 | `suki_memory.json::_meta.review_date` vs `today` | ≥2 天 | ⚠️ 標紅 + 列「最後成功 review_date」+ 建議查 review_cron.log |
| 📅 dailyreview cron | `~/Library/Logs/Marvin/review_cron.log` | 過去 7 天有 ≥1 次 `all 3 attempts failed` | ⚠️ 列出失敗日期 + 末段 stack trace（取 ❌ 那行 + 後 5 行） |
| 📦 feedbackbatch cron | `~/Library/Logs/Marvin/feedback_batch_cron.log` | 過去 7 天有 ≥1 次失敗 / 沒跑 | ⚠️ 列日期 + 錯誤 |
| 🗣️ speechdna cron | `~/Library/Logs/Marvin/speechdna_cron.log` | 過去 7 天有失敗 OR 週日後沒新檔 | ⚠️ 列 mtime + 上次成功日 |
| 💰 Gemini quota | review_cron.log 出現 `RESOURCE_EXHAUSTED` 或 `spending cap` | 任何 1 次 | ⚠️ 提醒 user 確認 ai.studio/spend cap |

### 報告位置

- 在 ritual 摘要的**最頂端**，先於昨日 feedback 摘要，因為 silent failure 高於日常觀察
- 沒有任何異常 → 一句話帶過「🚨 pipeline health: all green (review, feedbackbatch, speechdna, quota)」
- 任一異常 → ⚠️ 加粗 + 給修復建議 + 詢問 user 要不要立即補跑

### 自動修復邊界

- **safe 自動修**：純程式 bug（如 schema mismatch、type guard 漏）→ 先寫測試 → 修 → commit → 報告
- **需要 user 確認才動**：(a) 動 suki_memory 髒資料、(b) 跑 backfill（花 API quota）、(c) 改任何 launchd plist
- **只通知不修**：API 配額爆、Gemini 服務 outage、磁碟滿等外部因素

**How to apply:**
- 觸發點同 ritual：user 在 Discord-voice-bot repo 內提出第一個請求
- 5/24-5/31 那種「8 天才被人類發現」的失敗模式，這個檢查能在 5/24+1 天捕捉
- 健康檢查的 log 解析腳本若未來頻繁用，再 codify 成 `scripts/pipeline_health.py`（目前不過度抽象）

---

**週一加碼**（speechdna 週日 03:00 才跑）：
- 列出 `records/speech_dna_*.json` 的 mtime，挑昨天動過的 speaker
- 對每位動過的 speaker：跟前一份比 `style_summary` / `quirks` / `pause_proxies` 有沒有奇怪 drift（例如忽然 "drunk_mode" 主導、或 quirks 完全換一批）
- drift 異常 → 提醒 user，因為 LLM 模仿（imitation handler）會直接吃這份 dna

**摘要應該包含：**
- 昨日推薦總數 + sentiment 比例（positive / negative / neutral / skipped_immediately）
- 哪幾首歌被重複推薦過多次（重複數 ≥ 3）
- 哪幾位 user 的 negative 比例異常高 → 候選需要調整 taste profile
- audit 行裡 router 大量無回應 / invalid_json 失敗的訊號（pool / prompt 問題）
- 與**前日**或**本週同期**對比的 trend（必要時讀更早的檔）
- 📊 Dashboard mtime + 是否該 refresh（>7 天主動提）

**Why:** 2026-05-25 audit 發現 `feedbackbatch` 03:00 跑出來的 audit + summary markdown **沒有任何程序讀**——T1（music_memory feedback）、T2（suki 推進）是自動閉環，但 T3 audit + sentiment trend 是黑洞，等於每天花 LLM 算力跑分析但結果沒接起來改善。dailyreview 12:05 有 macOS notification 提示 user，feedbackbatch 沒有。User 明確要求由「每次 session 起手檢討」這個儀式來接起來，因為他本來就會跟我開 session。

**How to apply:**
- 觸發點：user 在 Discord-voice-bot repo 內提出第一個請求（任何議題）
- 例外：user 明確說「不要看 memory」或「直接做 X」、或請求是純技術問答（"這個 syntax 怎麼寫"）→ 跳過儀式
- 沒有昨日檔（bot 沒推薦 / batch 失敗）→ 一句話帶過「昨日無 feedback 資料」即可，不要捏造
- 摘要要簡短（5-10 行），可後續展開——目的是把訊號帶到 user 面前，不是 dump
- 摘要後如果發現**可改善的 system 訊號**（例如某首歌一直 negative、某 user 一直 skip）→ 主動建議下一步動作，不要等 user 問

---

## 📊 監控指標總表（校準階段 / Shadow 開工標準）

每日 ritual 跑下面這份清單，把每行的 **current** 填出來，與 **target** 比對：
任一行 current 達標 → 主動提醒 user「可以動 Stage X」。同步顯示於 dashboard
`records/marvin_status_dashboard.html` 內 📊 監控指標 details panel。

| Plan | 開工門檻 | 數據源 | 每日檢查指令 |
|------|---------|--------|------------|
| 🚑 Plan 10 LLM Rescue | shadow ≥30 + 抽樣正確率 ≥80% | `records/rescue_outcomes.jsonl` | `python scripts/analyze_rescue_outcomes.py` |
| 🪦 Plan 4 Intent Gap A.5 | **distinct (speaker,raw_query) ≥2** per intent_type | `records/agent_gaps.jsonl` | `python scripts/analyze_agent_gaps.py` |
| 🏁 Plan 1 Judge Race 五層修正 | ≥30/天 + 一致率 ≥92% | `records/judge_outcomes.jsonl` | `python scripts/analyze_judge_outcomes.py` |
| 📡 Plan 2 J2 Chat Veto | unique-win 率 ≥10% | `records/judge_outcomes.jsonl` | 同上 + 過濾 J2 行 |
| 📜 Plan 8 J1 Improvement | 連續 7 天 ≥30 樣本/天 | `records/judge_outcomes.jsonl` | 同上 |
| 💭 Plan 7 MemoryCallback | ≥1/天 callback win 連續 14 天 | `records/speak_outcomes.jsonl` | grep `callback_win` 計數 |

**規則**：
- 數據源檔不存在（如 Plan 10 剛上線、bot 沒對話過）→ 一句話帶過「📊 N: no data yet」即可
- target 用 absolute thresholds，禁加「我覺得應該夠」之類軟主觀判斷
- current 達 target 但**抽樣品質**未驗 → 仍提醒 user 但加註「需先抽樣標品質」
- 任何一條超過 7 天卡在 0% / 全 UNKNOWN → 主動提醒 user「這個觸發條件可能太嚴，要不要降閾值或改 metric」

## design_disciplines_for_future_consumers
*每條流程都會被別的 agent 取用、收 token fee — 兩條紀律保住擴展空間*

每條新流程都按這兩條紀律寫，但**不為想像中客戶過度抽象**：

**1. Pure core + IO shell**
演算法核心抽成 pure function（不碰 Discord state、不做 IO），IO 是薄薄一層 shell。
範例: `is_already_recommended(title, recent_titles)`、`build_autopilot_recommendation(...)`、`MoodSensor.current_vibe()`、`build_recommendation_pool(...)`。
**反例**: 在演算法裡直接 await `voice_client.play()` / 讀 `self.bot.cogs` / 摻 `time.time()` 進判斷。

**2. Schema versioning 從第一個 consumer 算起**
所有 record/event dataclass 加 `schema_version: int` 欄位。包含但不限於：
`Recommendation`、`FeedbackResult`、`speech_dna_<speaker>.json`、`audit_<date>.md` 行格式、`judge_outcomes.jsonl` 條目。
新增欄位 → bump version + 舊版 reader 容錯；不要 silent rename。

**Why:**
2026-05-25 對話結論——Marvin 護城河不是 code，是**封閉迴圈累積的資料**（group voice dynamics、taste profile、mood transition、feedback outcomes 一年累積，code 仿一週、資料仿不出來）。預期未來會把 curator 能力（recommend_music、current_vibe、speaker_profile）暴露成 MCP tool 收 token fee。現在不留出口、之後重構痛苦十倍。

**How to apply:**
- 寫新功能時主動套用——pure core 是免費的好設計，schema_version 是一行欄位
- **但**：別為「未來 consumer」造抽象層、SDK、interface protocol。沒有第二個真實 consumer 之前，保持單一 caller。只做這兩條，其他都等真有 consumer 再說
- Review 別人的 / 自己的 PR 時，看到 IO 摻進核心邏輯 → 提出來；看到新 dataclass 沒 schema_version → 提出來
- 若 user 要求「快速 hack」、明確說「這是一次性」→ 可以放寬第 1 條（但第 2 條成本太低，仍照做）

## feedback_audit_data_purity
*任何「≥N 筆達標」訊號要先驗質再信；測試污染 / fixture 重複 / 假觸發會讓指標看起來有進展實際沒有*

任何 plan 的「達標數字」（≥N 筆、≥X%）在拿去做決策前，**先 audit 資料純度**，別信表面值。

**Why:** 2026-05-30 一次 audit 抓出 3 個「表面有進展、實際沒有」的自欺訊號：
1. **測試污染**：多個測試建真 VoiceController 跑 wake path → 寫進真 `records/judge_outcomes.jsonl`(22% 垃圾)、`agent_gaps.jsonl`(78% 垃圾)。每跑一次套件灌 ~3 筆 fixture（`test query`/`今天天氣怎麼樣` 等）。
2. **假觸發**：Plan 4「16 non-UNKNOWN ≥5 達標」其實是同一句測試 fixture 重複 7 次 ×2，distinct occurrence=1。
3. **空轉**：J2 shadow wire 了但 env 沒設，0 筆卻顯示「已開」（見 feedback_env_gated_shadow_verify.md）。

清污後真實值：judge 122→74 筆、agent_gaps 62→9 筆、真實有機 gap≈0。

**How to apply:**
- 看到某 sink「達標」先跑純度檢查，污染特徵：
  - `ts < 1_000_000`（真 unix ts ≈17 億；小整數＝測試 fixture）
  - 同 `raw_query` 重複多次（QA 連發 / fixture）→ 用 distinct (speaker, raw_query) 算，不是 raw line count
  - `wake_intent=None` + 成對規律間隔 → 測試注入 signature
  - `raw_query` 命中已知 test fixture 集（`test query`/`今天天氣怎麼樣`/`記一下要買牛奶`/`那件事做完了`/`我剛才說了什麼`）
- 防線已建：`tests/conftest.py::_isolate_record_writes`（autouse 導 records/ 寫入到 tmp）+ `scripts/analyze_agent_gaps.py`（dedup 計數）
- 清污前**先備份**（records/_polluted_backup_<date>/），過濾後寫回，保留誤刪還原能力
- daily ritual 報數字時，若該 sink 近期有大量測試/QA 活動，主動加註「需 audit 純度」

## feedback_daily_probe_skill_decision
*check_plan_triggers.py 的 daily probe pattern 已成熟但暫不抽 skill / library；記何時該重新討論*

`scripts/check_plan_triggers.py` 已長出 5 個 check_xxx 函式共用同 pattern（file stat / jsonl 計數 / 回統一 dict {plan, title, trigger, current, met}），daily ritual 接線跑、output append 到 feedback_analysis 末尾。已過 Rule of Three 門檻、技術上可抽成 framework + gstack skill。

**2026-05-28 決定：暫不抽**。

**Why:**
- 受眾窄 — 「daily probe」工作節奏只在「資料驅動的 milestone、多 plan 並行、每個有 unlock condition」這類工程適用。純 CRUD / UI / 線性 ticket 開發用不上。Marvin 目前是這類專案的唯一實例。
- 抽出 skill 的最大價值是「跨專案習慣固化」（落地新 ML/research 專案直接套用），但目前沒有第二個專案在用。
- 內部 import 抽成 module 也只省 50% 重複，效益小、不抵維護介面成本。
- CLAUDE.md §2 簡單優先 + Rule of Three 真正要的是「3 個獨立使用點」，目前只有 1（Marvin 一個 repo 5 個函式不算 3 個獨立 use case）。

**How to apply (何時該重新討論):**
- 當第 2 個獨立專案需要 daily ritual probe 工作流 → 抽 gstack skill / library
- 當 Marvin 內 check_xxx 函式 ≥ 8 個或維護成本變痛 → 抽 Marvin 內部 module（不必 skill）
- 當「daily ritual」這個工作節奏被別人主動要求複製 → 寫成 skill 提供
- 否則：留在 `scripts/check_plan_triggers.py` 內，當作 Marvin-local 工具就好

## feedback_data_driven_diagnosis
*調查延遲、卡頓、故障時，先撈原始數據定位，禁止用「可能是 X」推測當結論*

調查效能問題（延遲、卡頓、UX 劣化、incident）時，**先撈原始數據逐筆看分佈 + 交叉比對，不要用推測當結論**。

**Why:** 2026-06-02 cleaner LLM p50 異常高，我第一時間推測是「queue backlog」。User 直接說「不要推測，用數據分析」。實際撈 STAGE_TIMING 逐筆才發現：cleaner 不是常態慢，是**時間窗事件**（22:49-00:04 健康 1-2s、00:10-01:35 爆 56-341s）；再交叉比對 llm_routing.jsonl 該窗 → 49% 失敗、429 主導、成功 call 最慢 115s；最後追到 code bug（llm_pool `_call` 無 timeout，掛住的連線無限等）。**推測（queue backlog）完全錯，數據指向完全不同的根因（429 風暴 + 無 timeout）。**

**How to apply:**
- 看到異常數字（p50 爆高之類）→ 先撈**逐筆原始值看分佈**，分清「常態 vs 少數極端值 vs 時間窗事件」，別只看聚合 p50
- 有對應的結構化 log（llm_routing.jsonl / judge_outcomes.jsonl / STAGE_TIMING）→ **交叉比對時間戳**，把症狀窗對到根因事件
- 一路追到**具體 code 行或 config**才算找到根因，不是停在「可能是 X」
- 要講假設時，明確標「這是待驗證假設」，並接著去撈數據驗證，不要把假設當結論報給 user
- 這跟既有 feedback_audit_data_purity（別信表面達標數字）同源：先信數據、先 audit，再下判斷

## feedback_env_gated_shadow_verify
*shadow/feature flag 接在 env var 後面時，wire code ≠ 啟用；0 數據可能是功能根本沒開不是沒流量*

接 env var gate 的 shadow / feature flag 時，**「code 接好」不等於「功能啟用」**——env var 沒在 launchd 路徑（`~/Library/Application Support/Marvin/run_bot.py`）設過，預設值會讓功能恆關，但 dashboard / 計劃卡常常已寫「已開」。

**Why:** 2026-05-30 發現 J2 Chat Veto 從 5/27 commit ed7a813 wire 後**空轉 3 天**——`voice_controller.py` 的 `os.getenv("MARVIN_SHADOW_J2_ENABLED", "false")` 預設 false，而 `run_bot.py` 從沒設這個 env，所以 J2 從未參與 shadow race，judge_outcomes.jsonl 0 筆 J2。Dashboard Plan 2 卻寫「shadow mode 已開」，daily ritual 每天以為在收 J2 資料。白等 3 天。

**How to apply:**
- Wire 任何 env-gated 功能後，**最後一步必須**：在 `run_bot.py` 加 `os.environ.setdefault(...)`（bot-only flag 放這，不放 `_launcher.py` 那是 5 cron 共用）+ 重啟 + `ps eww <pid> | grep <ENV>` 確認真的進 process
- Daily ritual 看到某 shadow plan **持續 0 筆**時，先分辨兩種 0：
  - 「沒流量」（bot 沒被觸發）→ 正常，繼續等
  - 「功能沒開」（env 沒設 / flag false）→ silent failure，要查 `ps eww` env + getenv 預設值
  - 判別法：同源其他 shadow（如 J1/J3 跟 J2 共用 judge_outcomes.jsonl）有資料但這個 0 → 幾乎確定是功能沒開
- 已知設在 run_bot.py 的 flags（2026-05-30）：`SPEAK_MEMORY_CALLBACK` / `MARVIN_INTENT_RESCUE_ENABLED` / `MARVIN_SHADOW_J2_ENABLED`

**同類教訓 — 低頻 cron 的修復必須當場手動跑過一次（2026-06-06）：** speechdna 只週日跑，commit `40bc9ad`（6/04）把 LLM 改走 bus 但漏了 `sys.path.insert(_ROOT)`，`from llm_pool import` 在 `python scripts/x.py` 路徑（sys.path[0]=scripts/）靜默 ImportError → try/except 吞掉 → 跳過 LLM → style_summary 留空。因週日才跑、commit 後沒人手動驗，**壞修復隱藏到下個週日才會炸**（又是無人值守、又是 silent）。手動 `--force --speaker 大肚` 一跑就抓到。修法＝補 sys.path（對齊 `analyze_daily_log.py`），commit `5cf0142`。**規則：改任何 daily/weekly cron 腳本後，別等 cron 自然跑——立刻用該腳本的單一 target 模式（如 `--speaker X --force`）手動 e2e 跑一次看真結果。**

## feedback_intent_gap_threshold
*agent gap 升級為「該寫 agent」的累計次數門檻，使用者偏好激進補 agent*

Intent gap clustering 的升級門檻 = 累計 **2 次**（不是我原本建議的 3 次）。

**Why:** 使用者 2026-05-27 拍板「滿二次就寫」，覆蓋我原本推薦的 3 次門檻。推測偏好：寧可寫了用不到，也不要等高頻證據累積（每多等一次都是「Marvin 假承諾」風險窗口）。

**How to apply:**
- Daily ritual 跑 clustering pass 時，cluster 的 `occurrence_count >= 2` 即標 `status="ready_to_implement"`
- 「累計 2 次」是**同類意圖**（cluster 內成員加總），不是字串完全相同 — clustering 機制本身要靠 LLM judge 把語意相近的 `intent_type` 字串歸成一群（embedding 對短描述容易誤判）
- 如果未來發現 noise 太多（雜訊也累積到 2 次），先檢討 gap_classifier 的 UNKNOWN 判定是否太鬆，而不是提高門檻 — 使用者偏好低門檻

## feedback_land_restart_no_ask
*使用者請求 land PR / 重啟 bot 時，CI 綠就直接做，不要每次問「要不要 land / 要不要再重啟」*

使用者請求「land / 推上去 / 重啟」時，**CI 綠 + mergeable 就直接執行整套**，不要逐 PR 或逐次重啟前再徵詢確認。

**Why:** 2026-06-04 一連串修正（PR #6-#10）每個都先問「要不要 land」「要不要再重啟一次」，使用者明確說「下次不用問了」。他信任這個流程，重啟中斷語音是已知且可接受的代價。

**How to apply:**
- 標準 land 流程：`gh pr checks` 確認綠 → `gh pr merge --rebase --delete-branch` → `git checkout main && git reset --hard origin/main`（rebase 重寫 SHA，本機要硬對齊，先驗內容無遺失）→ 重啟 bot（`launchctl kickstart -k gui/$(id -u)/com.antigravity.marvin.bot`）→ tail log 確認啟動無 traceback。
- 多個 PR 一起 land：彼此無檔案重疊就依序 rebase merge，不用逐個問。
- **仍要 surface 的例外**：CI 紅 / merge conflict / 重啟後 bot crash loop / 改動牽涉破壞性或不可逆操作 → 這些照常停下報告。
- 重啟只影響 live bot 進程（載入新 import）；cron 類（daily review / speechdna）每次從磁碟讀新碼，不用重啟。關聯 `bot_run_topology`。

## feedback_llm_bus_model_staleness
*Cerebras/Groq/OpenRouter 等 free tier 的 model ID 會 deprecate；bus 內 hardcode 一段時間後會悄悄全 404*

LLM Bus（`llm_pool.py:_PROVIDERS`）裡 hardcode 的 model ID 會被 provider 端 deprecate，**而且 bus 失敗回 `''` 不 raise，caller 看到的是 silent empty content**——這條反饋路徑超痛，PoC 上線當天才會發現。

**Why**：2026-06-01 Marmo 一搭一唱 PoC live 測踩到：Cerebras 把 `llama3.1-8b` + `qwen-3-235b-a22b-instruct-2507` 都下架了，bus 內這兩條都 404 `model_not_found`。session_summarizer 也踩過同樣坑（log 翻得到），但因為它走 silent failure / fallback，沒人盯就一直被吃掉。

**How to apply**：
- **新 caller 進 bus 前先快速 sanity**：`curl -H "Authorization: Bearer $KEY" https://api.cerebras.ai/v1/models`（或對應 provider 的 endpoint）確認 `llm_pool.py:_PROVIDERS` 裡的 model 還在
- **bus dispatch 寫 `success=True` 不代表內容非空**：CerebrasAgent 已加 empty-content warning log（6/1），但其他 provider agent 沒有。下次撞 silent failure 第一手檢查 `records/llm_routing.jsonl` 看 provider/model/latency；若 latency 異常短（< 1s）+ success=True、大概是空回，再去 grep agent log 確認
- **Cerebras 的 reasoning model 要 max_tokens ≥ 2048**：gpt-oss 系列每次吃 150-700 reasoning tokens，1024 預設不夠長 Chinese JSON 輸出。CerebrasAgent 已改預設 2048，但 caller 顯式傳更小 max_tokens 仍會中招
- **掃 model ID 的時機**：每次新 PoC 上線前、或任何 LLM caller silent failure 排查時、最遲季掃一次。`session_summarizer` 出現空回是強訊號
- **不可 bypass bus 偷打雲端 API**：bus 是 shared infra（quota / latency tracking / bid loop / fallback chain），繞過去等於放棄 telemetry + 容錯。修 bus 才是正解（Jack 6/1 原則）

## feedback_llm_calls_must_use_bus
*需要呼叫 LLM 時一律走 llm_pool（bus），禁止 caller 自開 client / 寫死 model ID*

**需要呼叫 LLM 時，強制走 LLM bus（llm_pool）統一管理 model，禁止任何 caller 自開
client 或寫死 model ID。**

**Why:** model 寫死散落各檔 = 每個都是地雷，過期/配額爆就獨立炸、bus 的
fallback/cooldown/timeout 一點幫不上。2026-06-02 incident：`analyze_daily_log` 自開
`genai.Client` + 寫死 `MARVIN_REVIEW_MODEL`，pro 免費層配額爆 + 舊 flash-preview-05-20
已 404 → suki_memory daily review 卡 **10 天**不更新，Marvin 一直翻 5/22 舊話題。
同類前科：Cerebras `llama3.1-8b`/`qwen-3-235b` 404、Gemini pro 429。bus（llm_pool）
本來就集中管 model（ProviderSpec / _PAID_REVIEW_MODELS）+ 多 provider fallback +
per-call timeout，全走它就不會單點炸。

**How to apply:**
- **小 / 即時 call**（cleaner / classifier / judges / sentiment 分析）→ `build_tiered_router()`
  的 `quick` / `analyze` tier（免費池，OpenAI-compat，多 provider fallback）
- **大型 batch**（daily review 等 ~67k prompt、大結構化輸出）→ `llm_pool.call_paid_review()`
  （付費 Gemini，**genai 原生 SDK + thinking_budget=0**）
- model ID 只能加在 `llm_pool`（ProviderSpec / `_PAID_REVIEW_MODELS`）一處，**禁止**在
  caller 寫 `genai.Client(...)` / `model="gemini-..."` / 直接 groq client
- 寫新功能要呼叫 LLM 時：先問「這是小 call 還大 batch」→ 選對應 bus 入口，不要自己接 SDK

**關鍵 gotcha（thinking 模型）：** gemini-2.5-flash 是 thinking 模型。OpenAI-compat 端點
把 thinking token 算進 max_tokens，大 input 下 thinking 吃光額度、output 被腰斬（實測
finish=length、output 僅 629 token、JSON 截斷）。所以大型結構化輸出**必須走 genai 原生
SDK + thinking_budget=0**（額度全給 output，實測 7201 token 完整），不能走 OpenAI-compat。
這就是 call_paid_review 用 genai 而非 OpenAI-compat 的原因。

相關：[feedback_llm_bus_model_staleness] model ID 會過期要定期掃；本條是「源頭就別在
caller 寫死、一律走 bus」的預防規則。

## feedback_mock_dont_self_fixture
*驗 LLM 或外部系統表現時，不能自己手寫 sample 當 baseline——測的是自己的能力不是產品的能力*

驗 LLM / AI 生成系統 / 任何「目標是讓 X 產出 Y」的工具時，**不要自己手寫範例當 baseline 或 PoC 前的試水**。要用同一個 X 生成，自己只當審核者 / curator。

**Why**：2026-05-31 Marmo 一搭一唱 PoC 的 pre-flight mock 我建議「Jack 手寫 4-5 組對白丟朋友看當 F1 gate」，Jack 抓到 trap：手寫好不好笑測的是 Jack 寫不寫得好，**不是** LLM-as-comedian 走不走得通。如果手寫好笑、LLM 寫不好笑、F1 gate 漏判 → PoC 繼續往火裡跳一週才發現問題。

**How to apply**：
- 任何「測 X 的產品效果」之前先問：「**這次測試的生成者是 X 還是我？**」生成者是我 → 立刻改用 X
- 我（Claude / 任何 LLM）在這類測試裡的合法角色是「**critique / curate / 提原則給人類審**」，**不是** producer
- 適用範圍：LLM 對白測試、prompt 評估、AI 生成內容 PoC、推薦系統 baseline、persona 評估、任何「外部工具會做的事我替它做」的場合
- 第二條原則：若還沒有 X 可用（例如 PoC 前），用「**最接近 X 的替身**」——當下 session 的 LLM、現成 model（GPT/Claude/Gemini）、open-source baseline。**手寫永遠是最後選項**，理由是手寫 = 用自己當 oracle

## feedback_trigger_excludes_sentinels
*計算「累積 ≥N 筆觸發 next phase」這類門檻時，必須排除 sentinel state（UNKNOWN / 0.0 / no_match 等合法 negative）*

寫「累積 ≥N 筆觸發 next phase」這類自動 trigger 時，必須先想清楚計數是否要排除 sentinel state（UNKNOWN intent / 0.0 confidence / `reason="no_match"` 之類合法 negative output）。

**Why:** 2026-05-28 Plan 4 (Intent Gap A.5 Clustering) 觸發條件原本是「`agent_gaps.jsonl` ≥5 筆」。當天累積到 5 筆觸發，但人工檢視發現全是 `intent_type=UNKNOWN`（classifier 對「無意圖雜訊」的合法輸出）。clustering 對 UNKNOWN 永遠是空 cluster — 觸發了等於沒觸發，浪費下一個 session 的 token。修法是改成「`intent_type != "UNKNOWN"` ≥5 筆」。

**How to apply:**
- 寫 `scripts/check_plan_triggers.py` 或類似 trigger 條件時，先看資料源 schema 有沒有 sentinel/negative state（contract 通常寫在 dataclass docstring）；如果有 → 計數時 filter 掉
- 同類陷阱：J1/J2/J3 dense bid 用 `confidence=0.0 + reason="no_match"`（見 CLAUDE.md IntentBus 規範）。如果未來寫「J2 unique-win 率」之類統計，dense 0.0 不該算「J2 有贏過」
- 觸發測試一定要寫「全 sentinel」case（如 test_intent_gap_clustering_excludes_unknown），不只測 happy path
- 不要修 sentinel 本身去 work around — sentinel 是 contract 的一部分（讓 log / verifier 看得到「我看了不是我」），改的是 trigger 計數邏輯

## skip_signal_attribution
*收到 skip 訊號時調整推薦邏輯，不是把歌標 blacklist*

推薦音樂被 skip 時，**要調整的是「如何不產生這種推薦」，不是把那首歌標記**。

**Why:** Skip 是訊號，歸因方向決定後續行為。把錯誤歸給歌曲 → blacklist 一刀切，這首歌永遠不會在不同心情/情境下回來；歸給推薦邏輯 → 問「為什麼這次推薦這首」，可以修上游（mood mismatch / 同質性過強 / 時段不對 / 主題切換）讓未來推薦更準。同一首歌在 chill night 適用、在熱聊高峰可能就 skip，這跟歌沒關係。

**How to apply:**
- 看到 skip data（單筆或 aggregate）→ 第一個問題是「推薦器當下看到什麼 context 才決定送這首」，不是「這首該不該下架」
- 不要在 handler 內加「N 次 skip → blacklist」這種 hard-coded 規則；現有 `intent_agents/playback_control_agent.py` 是這個 antipattern 的活實例：
  - line 34: `SKIP_BLACKLIST_THRESHOLD = 2` 常數
  - line 163-164: handler 內 `if len(spk_set) >= SKIP_BLACKLIST_THRESHOLD: self._add_to_blacklist(...)`
  - line 208: `_add_to_blacklist` 把 url 寫進 `CoverBlacklist`（hard ban）
  未來重構：訊號送回 recommender / curation resolver；blacklist 只保留 user 明確說「這首爛」的手動入口
- 設計 telemetry 時，skip event 要帶 context（mood snapshot / speaker / 前一首 / round position），讓離線分析能回答「什麼樣的推薦會被 skip」
- 對話中如果 user 講「為什麼一直放這種歌」，不要把該歌加 blocklist；改 curation 偏好或 round size

Cover blacklist 仍可保留**手動**入口（user 明確說「這首爛」），但**自動**規則應該往「降低被選機率」設計，不是 hard ban。


---

# 🗂️ Project — 進行中的工作、目標、決策

## audio_per_song_loudnorm
*「音量大小不穩定」抱怨時——Plan 12 音樂層怎麼做 per-song loudness 正規化、為何不用動態 loudnorm*

**症狀**：使用者反覆抱怨「音量大小不穩定」。根因＝Plan 12（現行模式）音樂路徑（voice_controller `play_stream_song` 的 no-DJ branch）**完全不做 loudnorm**——因為動態 single-pass `loudnorm`/`dynaudnorm` 會在歌內 pumping（安靜段越推越大→漸進破音，使用者實測「越播越 distorted」「悶」），所以之前整個拿掉。但拿掉後歌與歌之間響度差很大（實測知影饒舌 -7.9 LUFS vs 順子回家抒情 -19.5 LUFS，差 11.6dB），同音量%下大聲炸耳、安靜聽不到。

**解法（2026-06-04 PR #15，四點）**：
1. 按鈕步進 `cogs/voice_views.py PlayControlView.VOL_STEP` 10%→5%（細調；語音 `volume_agent.VOICE_VOL_STEP` 仍 10%）。
2. 背景取樣歌曲 **25/50/75% 三點**（`_measure_norm_gain_bg`，ffmpeg `-ss POS -t 20 -af ebur128`，create_subprocess_exec 不阻塞播放）。比量整首快、能早點套。
3. `loudness_norm.compute_loudness_gain`：average LUFS → 到 -14 LUFS 的**常數**線性增益（clamp 0.25~4.0x），存 `_stream_norm_gain[url]`。mixer 同步迴圈（`_mixer_play_music` volume_attr 那行）乘進使用者音量。
4. **每首只量一次**（dict 有就 return），常數增益**不在歌內 pumping**＝不重蹈動態 loudnorm 覆轍。失敗/逾時→1.0 raw（graceful）。

純函式（gain/取樣位置/ebur128 解析/平均）在 `loudness_norm.py` 可單測。live 驗證：知影→0.49x 壓低、回家→1.89x 提高，拉到 ~-14 LUFS 一致。

**注意**：只在 Plan 12 mixer 路徑生效（`self._plan12`）。legacy 非-plan12 路徑另有動態 loudnorm（line ~7694）+ `_measure_loudness_bg`（給 hotswap Slice 2，量整首存 `_stream_loudness`，欄位不同別混用）。關聯 `project_plan12_local_mixing`。

## bot_run_topology
*launchd → wrapper → main_discord.py 啟動鏈、wrapper 跟 venv 的位置、cwd 必須正確*

Bot 由 launchd 託管，**不是** systemd / docker / 手動 `python main_discord.py`。

```
launchd
  → ~/Library/LaunchAgents/com.antigravity.marvin.bot.plist
      → /usr/bin/python3 (系統 3.9, 只跑 wrapper)
          → ~/Library/Application Support/Marvin/run_bot.py
              → _launcher.py::run_with_retry
                  → venv_simon/bin/python3 (3.13) main_discord.py  ← 真正的 bot
```

**關鍵路徑**（不要硬編碼到別處）:
- WORKDIR: `/Users/jackhuang/Code/Discord-voice-bot`
- venv: `/Users/jackhuang/Code/Discord-voice-bot/venv_simon/bin/python3` → python3.13
- wrapper: `~/Library/Application Support/Marvin/{_launcher.py, run_bot.py, run_cloudflared.py, run_daily_review.py, run_daily_slice.py, run_speech_dna.py}`
- 5 個 launchd job 都用 `_launcher.py` 跑：`com.antigravity.marvin.{bot, cloudflared, dailyreview, dailyslice, speechdna}`

**Why**:
- 2026-05-24 從 `~/Documents/Antigravity/Discord-voice-bot/` 遷移到 `~/Code/Discord-voice-bot/`
- 遷移後 `_launcher.py::WORKDIR` 還寫死舊路徑，5 個 cron job 全跑舊路徑寫舊 DB
- 同時 `com.marvin.stream-524.plist` 是 self-destruct 的單次 cron（已過期 + 殘留），bootout 後刪除

**How to apply**:
- 改 launchd 設定要動 **plist** + **wrapper script**，兩處都要更新路徑
- 不能用 `restart_bot.sh` 重啟 bot — 那是手動工具，會啟動 orphan 跟 launchd 託管的撞 gateway。要重啟用 `launchctl kickstart -k gui/$(id -u)/com.antigravity.marvin.bot`
- 找問題先 `lsof -p <PID> | grep cwd` 確認 bot 在哪個資料夾跑、`pyvenv.cfg` 看 Python 版本
- launchd 託管程序的 `ps` PPID=1（被 launchd 收養），手動跑的 PPID 是 shell

**Anti-pattern**:
- 改 plist 後忘了 `launchctl bootout` + `bootstrap` 重新註冊 → 改的設定沒生效
- 看到 `ps` 有兩隻 bot 不要急著 kill 兩個 — 殺 PPID=1 那個 launchd 不會幫你重啟（如果是 launchd 託管的反而會），先看 PPID

## ci_red_2026-06-03
*CI 連紅 13 failed 全修綠（commits 1c02c3a + c3b1635，CI run 26891033996 三 job 全 success）*

**✅ 完全綠（2026-06-03，CI run 26891033996 三 job 全 success）。** 分兩個 commit 修：

**commit 1c02c3a** 修 11 個（本機 3.12 venv 重現）：
- production: `RealtimeVADSink.__init__` 的 `asyncio.get_event_loop()` 被 pytest-asyncio teardown 後無 current loop 放大成 setup ERROR → 改 `get_running_loop()`+fallback
- 測試隔離: track_quality 共用 `/tmp/.never-exists.json` 污染 → 改 tmp_path fixture
- 測試 bug: find_song 用 `get_event_loop().run_until_complete()` → 改 async；bridge_wiring mock 缺 `stream_mode`

**commit c3b1635** 修最後 2 個（push 後 CI 仍紅才抓到）：
- **🔑 真 production bug：LLMBus degraded 告警的 `_last_degraded_ts` init 為 `0.0`，debounce 比 `time.monotonic()-ts < 300`。但 `monotonic()` 不是 wall-clock 是開機起算——剛開機的 CI 容器 monotonic 可能 <300，第一次告警被誤判重複而 debounce 吞掉。久開機本機 monotonic 巨大 → 永遠不重現。改 `float("-inf")`。**

**兩大教訓**：
1. 本機 3.13 綠 ≠ CI 3.12 綠；驗 CI 要對齊 3.12 + 跑整包（建 `/tmp/ci312` venv，注意 /tmp 會被系統回收）。
2. **時間相關測試別用 `time.monotonic()` 的絕對值當 sentinel**——monotonic 起點隨機器 uptime 變，剛開機機器（CI）行為跟久開機機器（dev）不同，是「本機綠 CI 紅」的隱形殺手。sentinel 該用 `-inf`。

2026-06-03：CI 曾**連紅 5+ commits**（run 26870762749：`13 failed`）。與 docs commit (30988ce) 無關。一開始選「只報告」，後改動手修。

根因分兩類：

**A 類 — Python 3.12 vs 3.13 行為差異（本機 3.13.5 過、CI 3.12 掛，~10 個）**
- 核心：`discord_voice_engine.py:264` 的 `self.loop = asyncio.get_event_loop()` 在無 running loop 時，3.13 只丟 DeprecationWarning，**3.12 raise `RuntimeError: There is no current event loop`**。
- 牽連：`test_vad_adaptive`(3 err) / `test_twitch_stt_groq_fallback`+`test_twitch_stt_retry`(5) / `test_llm_bus_daily_and_degraded`(2) / `test_find_song_agent` lyrics slot(2)。
- 修法方向：建構時別 eager 呼叫 `get_event_loop()`，改 lazy 或 `get_running_loop()` fallback。動到 runtime。

**B 類 — 跨版本都壞、真實 drift（本機 3.13 也重現，3 個）**
- `test_track_quality.py::test_invalid_url_fail_open` / `test_api_error_fail_open`：`assess_track_quality()` 對無效 URL 回 `"ok"`，測試期望 `"invalid_url_fail_open"`。程式改了行為 vs 測試過時，需確認意圖。
- `test_bridge_wiring.py::test_voice_state_leave_emits_member_left`：member_left 事件沒發出。

注意本機 Python 是 3.13.5，CI 是 3.12——本機綠不代表 CI 綠，驗 CI 類問題要對齊 3.12。相關 `daily_feedback_ritual` 的 pipeline 健康檢查。

## cleaner_latency_and_response_failrate
*「Marvin 遲鈍」抱怨或查 latency 時——cleaner 慢的根因+已加的預算控管，以及還沒解的回應 LLM 64% 成功率*

2026-06-04 跑 3am ritual 數據健檢（`records/latency_breakdown_<date>.md`，3am 由 analyze_latency_breakdown.py 產）撈出兩條**長期**病（4 天趨勢，非單日）：

**1. cleaner LLM 慢（PR #20 已修，但「7s」其實是量測假象——2026-06-05 更正）**
- ⚠️ **「cleaner p50 7s / p90 19-27s」是量錯的，不是 cleaner 真實耗時。** 2026-06-05 ritual 追到 code 行：`latency_breakdown` 的 cleaner 段＝`cleaner_done − stt_done`，但這兩個 mark **跨越 query_queue 邊界**（stt_done 在 `discord_voice_engine.py:1378` STT 完成時打、cleaner_done 在 `voice_controller.py` worker dequeue 後的 `_confirmation_flow` 尾端打）。所以該段＝**排隊等 worker（≤25s，`_LATE_RESPONSE_SKIP_SEC`）＋ evt.wait 等使用者講完問句（≤10s）＋ 真 cleaner**。真 cleaner 被硬封：confirm 路徑外層 `wait_for(_CONFIRM_CLEAN_TIMEOUT=2.5s)`、bus 路徑 PR#20 的 6s 預算。**PR#20 預算有效，cleaner 本身不慢**；體感遲鈍的大宗是 **worker 排隊**（多人/狗與露 autopilot 洗 query_queue 時）。
- 證據：6/4 那筆 19163ms（`麻煩播放費玉清的晚安曲`）是 PR#20 部署前(17:09)的 00:43 舊 code；6/5 00:01 那筆 11348ms 是新 code 但 strip 後≥4字跳過 evt.wait → 11s 幾乎全是排隊。
- **已修量測（2026-06-05，本 session）**：`pipeline_timing._STAGES` 加 `dequeued`/`question_done` 兩個中間打點，`analyze_latency_breakdown.stage_durations` 把舊單一 cleaner 段拆成 `queue_wait`/`question_wait`/`cleaner_pure`（無中間打點的舊行/nowake route 仍走 legacy `cleaner` 向後相容）。TDD：test_pipeline_timing + test_analyze_latency_breakdown。**下一份 latency_breakdown 應能看出遲鈍到底卡 queue 還是清洗。**
- PR#20 本體仍在 main（commit 90fce87）：`stt_cleaner.clean_stt_text` 每段 timeout 8→4s + 總預算 6s。配額爆（Groq TPD / Gemini free 500/天）→ provider hang 仍是真實壓力源，但被預算兜住。

**2. 回應 LLM「64% 成功率」——查清是污染+誤解（PR #21）**
- 2026-06-04 查 `llm_routing.jsonl` 真相：①**測試污染**——24h 287 筆有 129 筆是測試（test_llm_bus_flag/parity 跑 dispatch 用 test-name 當 purpose，經 `llm_agents.metrics.log_dispatch` 寫死的 `_LOG_PATH` 寫進 prod，模擬 429/no-available 假失敗）。已補 conftest `_isolate_record_writes` block #5 patch `_LOG_PATH`→tmp（驗證 618 測試後 prod 行數不變）+ scrub 100 筆。②**指標誤解**——清後真實 bus 成功率 ~61%，但**失敗全是背景任務**（extract_emotional_moments/_analyze_song_reactions/generate_player_farewell…）撞 429＝free 池配額壓力，graceful 不影響 UX。
- **關鍵**：**主回應（Marvin 答使用者）走 `stream_llm` dedicated client、繞過 bus、不在 llm_routing.jsonl** → 這指標**根本沒測到主回應**。「64%」不代表使用者體感回應在失敗。
- **真要知道主回應失敗率** → 得 instrument `stream_llm` 路徑（bypass 債，`project_llm_pool_attribution`）。背景 429 壓力則靠 attribution #3 背景降權 + PR#6 Gemini 2.5 free 池 headroom。

關聯 `daily_feedback_ritual` `feedback_data_driven_diagnosis`（兩次都是「追到 code 行才下結論」：6/4 原以為 cleaner 沒走 bus→trace 發現有走；6/5 原以為 cleaner 慢→trace 發現是量測跨 queue 邊界、真 cleaner 被硬封、大宗是排隊）。下次看 latency_breakdown 直接看拆出來的 `queue_wait`/`question_wait`/`cleaner_pure` 三段，別再被混合 cleaner 段誤導。

## cryptoerror_storm_sentinel_blindspot
*改 Discord 頻道 bitrate → 重連金鑰沒同步 → CryptoError 風暴 → STT 糊；Sentinel 30s 寬限期盲點害自癒不觸發*

2026-06-04 incident：玩家開台（持續放歌）時「喚醒點歌沒一次成功」。根因兩條疊加。

## 因果鏈
1. **觸發**：有人把 server 語音頻道 bitrate 改成 96k。改 bitrate 會強迫所有 client **重新協商語音連線**（新 session description / secret_key）。bot voice_recv 重連後沒乾淨拿到新 `secret_key` → inbound 封包持續解不開。
2. **症狀**：`discord.ext.voice_recv.reader: CryptoError decoding packet data` 從 6/3 傍晚爬升、6/4 00:00 起 **~80/min**（正常 0-2/min）。STT 還在出字但**糊**（「中九筆的樣子我比怪怪的」），喚醒/點歌指令轉錄不準 → 點歌失敗。
3. **解法（手動）**：dismiss → `launchctl kickstart -k gui/$(id -u)/com.antigravity.marvin.bot` 物理重啟 → /summon 重進。CryptoError 立刻歸零、STT 出字乾淨（「我有買了一支加百裕，那個比較穩定」）。**證實乾淨握手=乾淨 secret_key=解密正常**。

## 信號（下次秒判）
- STT「突然變糊」+ `CryptoError` 速率暴增 → 先問「有人動頻道 bitrate / 設定嗎」。
- 撈：`grep CryptoError bot_main.log | 每分鐘計數`；對照成功點歌時段的速率（正常 0-2/min）。

## ✅ 已修（commit 74b78cd「Sentinel 寬限期盲點」，2026-06-04，session 前就在）
正解已落地：`report_sink_error` 改用 `_dave_grace_should_forgive`（voice_controller.py:926）——
寬限期只在「連線後 early_s(15s) 內」或「last_decrypted_audio_time >= connection_time（自連線
已成功解密 ≥1 封包）」才豁免；過 15s 卻零成功解密 → **不豁免、讓錯誤累積升級**，繞過盲點。
`last_decrypted_audio_time` 在成功解密更新（discord_voice_engine.py:274/340）。測試
`tests/test_sentinel_dave_grace.py` 4 case 全綠（含「零解密不豁免」關鍵條）。2026-06-04 驗過 fix 在。
**下面是原始診斷（保留供參），bug 本身已解。**

## ~~自癒機制存在但今天沒觸發（真 bug，待修）~~（已修，見上）
bot 本有兩層自動換金鑰，**但都沒救回**：
- **封包級**：`discord_voice_engine.py` 的 `[KeySync]` patch——CryptoError 時重讀 `voice_client.secret_key` 重試。但若 voice_client 自身的 key 就是壞的，重讀無用。
- **Sentinel**：`voice_controller.py` `report_sink_error`→`orchestrate_recovery`：DAVE 錯誤計數 ≥3 → soft-repair(重新加入頻道，2次) → 物理重啟。

**沒觸發的精確原因**：`report_sink_error`（~930 行）有 **30s DAVE 寬限期**：`if now - connection_time < 30: return`。今天連線不穩（23:47/23:53 兩次「無感測音訊軟修復」各自重連刷新 `connection_time`）→ 寬限期一直 reset → CryptoError 全被當「同步等待中」吞掉 → `dave_error_count` 爬到 2/3 就被重置、**從沒到 3** → 升級到物理重啟永遠沒跑。**inverted 邏輯：連線越不穩、自癒越不啟動。**

**正解方向**：寬限期內若 `last_decrypted_audio_time` 一直沒更新（收到封包卻零成功解密）→ 不是同步延遲、是真壞 → 直接升級重啟，繞過寬限期。測試骨架見 `tests/test_sentinel_monitor_loop.py`。

## iba_t0_wakeless_music
*「點歌沒反應/放錯歌」排查時先確認 IBA-T0 wakeless 路徑有沒有 fire、query 是不是被 STT 糊掉*

即使喚醒詞被 debounce/判錯，**無喚醒詞點歌（IBA-T0）救援本就存在且運作中**，不要以為意圖被丟掉。

路徑：`process_debounced_speech`（cogs/voice_controller.py）→ `_detect_music_direct_command` 偵測 play 命令 → `build_nowake_play_ctx` 重建成「播放{query}」→ `_intent_bus.dispatch` → MusicAgentV2 三檔分流（SPECIFIC/CURATION/DIRECTIONAL）。log 訊號：`🎵 [IBA-T0→Bus] {speaker} no-wake 點歌進 bus | query=...`。糊掉的喚醒前綴（如「馬馬文波馬文播放李欣」）會被 `_extract_music_search_query` 正確剝成「播放李欣」。

**2026-06-04 調查 陳進文「喚醒失敗」發現兩個真 gap（非「意圖沒捕捉」）：**
- **Gap A（已修，PR #9）**：`_detect_music_direct_command` 的長度閘 `_IBA_T0_MAX_LEN=15` 把 >15 字整句拒絕（防 5/18 control-word substring 誤觸），誤砍「夾在閒聊後的明確點歌命令」（陳進文「這樣妹妹說 曉雯幫我播放，孫淑媚的愛人」~17字被整句丟）。修法 `_detect_embedded_play`：長句句尾找 play kw，**tail 必須含 music marker（的/歌/曲/音樂/MV）才救**（比短句 gate 嚴，避免誤吞「我想聽你說完之後…」）。只救 play，control 詞長句不救。
- **Gap B（已修，PR #10）**：STT 糊字 → resolver 搜錯。「播放李欣」搜到脫口秀「李新對公婆講話」（非歌）。根因 `music_search.pick_best_music_candidate` 舊邏輯「全負分仍回最高者（總比沒結果好）」硬塞非音樂 + 非音樂靠時長 +3 拿正分。修：新增 `has_music_signal`（Music 類別 / "歌手 - Topic" / official/MV/cover/歌詞 hint；黑名單覆蓋弱 hint），pick_best 只選帶音樂信號者，全無→None（caller voice_controller:6775 當無結果 graceful）。取捨：素人無標記歌可能誤拒（Jack 選嚴格擋非音樂）。關聯 `skip_signal_attribution`。

排查口訣：「點歌沒反應」先 grep `IBA-T0→Bus` 看有沒有 fire——有 fire＝意圖捕捉 OK，往 query 品質/resolver 查（Gap B）；沒 fire＝看是不是長度閘砍掉（Gap A 類）或 `_query_implies_music_intent` 沒過。

## j1_improvement_loop
*regex 本身不自學，但決策能力可透過三條工程化迴圈隨 outcome 資料優化*

**規劃（尚未實作）**：parallel judges race 的 J1 RegexJudge 是純 regex，本身不會自學。但「決策能力」可以隨真實 dispatch outcome 資料優化，三條迴圈：

| 路徑 | 怎麼學 | 自動化邊界 |
|---|---|---|
| **(a) Confidence 校準** | 紀錄 (J1 winner schema, 真實 dispatched intent 是否正確 / 使用者有沒有立刻 cancel / 5 秒內有沒有重複指令) → per-schema success rate → 偏差大時調 confidence | **只允許自動下調**；上調必須人工 approve（避免 LLM 自我強化錯誤、dispatch side effect 不可逆） |
| **(b) Schema 挖掘** | J1 miss 但 J3 (cleaner+bus) hit 的 case 收集 (raw_text, final_intent) → 離線小 LLM 提案新 regex pattern → **人工 review** 合進 `intent_agents/*_agent.py` | 純離線，零 runtime 風險 |
| **(c) Keyword 擴充** | `intent_agents/constants.py` 的 `MUSIC_SKIP_KW` / `STRONG_PLAY_KW` / `WEAK_PLAY_KW` 從挖掘結果擴充同義詞 | 同 (b)，純離線人工 review |

**Why:** J1 命中 → 直接 dispatch（換歌、TTS、API 呼叫），錯了不易回滾。閉環中必須有人類；自動校準只走保守方向（下調 confidence）。Schema/keyword 擴充純離線人工 review，不影響 runtime。

**How to apply:**
- 前提：先補 instrumentation——`records/judge_outcomes.jsonl`（utterance_id, j1_winner_schema, dispatched_intent, user_cancelled_within_5s, user_repeated_within_5s）
- 順序：先靜態跑 J1 收 outcome → 再做 (b)/(c) 離線挖掘迴圈 → (a) 自動下調最後上線
- 別在 J1 內偷塞 LLM「自我糾正」邏輯——破壞 J1 <5ms 預算 + 違反「judges 之間獨立」race 契約

**未解設計問題**：
- judge_outcomes 的「正確 dispatch」標籤怎麼自動推斷（使用者沒明說錯了的時候）
- (a) 自動下調的觸發門檻（per-schema sample size、信心區間）
- (b) 挖掘出的 pattern 該歸到哪個 agent（cross-agent 衝突怎麼處理）

---

## 2026-05-27 分析確認的具體 J1 schema 修正項

49 條 shadow 樣本人工逐條判讀後（報告：`records/judge_outcomes_analysis_2026-05-27.md`），
四個確定要動的 J1 修正：

1. **Threshold 0.90 → 0.85**：8 條 `weak_play_curation` 0.85 卡下緣且 J1/J3 一致 → 直接降
2. **Skip negative context**：`control:skip` 對「應該/為什麼/怎麼/是不是」句首 → bid 0.0 with `skip_in_question_context`（L19、L32 兩條 FP）
3. **weak_play_specific 排除非音樂類名詞**：matched slot 若為「網站 / 影片 / 文章 / 圖片」→ negate（L48 FP「幫我找...線上網站」）
4. **裸歌名 / 裸藝人名 fallback bid**：cleaned text path only，bid 0.50~0.60，避免污染 raw（L37「始作俑者」J3 dense zero）

優先順序：A (race 規則 guard 不 commit) → 1 (threshold) → 2/3 (regex 修正) → 4 (cleaned fallback)

## judge_followup_actions_2026-05-27
*49 條樣本後 5 條修正執行狀態 + 6/1 重收數據驗證標準*

## ✅ 6/3 數據分析已跑（報告 records/judge_outcomes_analysis_2026-06-03.md）

147 條樣本。**真實結論：沒回歸，反而進步** —— 語意一致率 96.4%（>92% 目標）、
fast-path 39.5%（持平、未達 50% 因 `j3_cleaner_precomputed` 新路徑搶贏 60.5% 把 J1 cancel）。

**⚠️ 大教訓**：第一次跑腳本吐「一致率 49.5%」像災難，**是腳本 bug 不是回歸**——
腳本不認得 race 的 `cancelled` 狀態（precomputed J3 先到把 J1 取消，35/147 條）也不認得
`guard(0.96)≡cleaner_judge(0.00)` 都是 NO_INTENT。已修 `scripts/analyze_judge_outcomes.py`
（_outcome 語意分桶 + 只配對兩邊 completed）+ `tests/test_analyze_judge_outcomes.py` 14 條鎖住。
**這是 `feedback_audit_data_purity` 的活案例：表面達標/未達標數字都先 audit 純度再信。**

**行動項處理（6/3）**：
① ✅ **J2 觀測性修好**：診斷出 J2 是 J1 外的 veto wrapper（非獨立 judge），確認真意圖時
   bid_reason 零痕跡 + 所有失敗（timeout 0.5s/exception/404）fail-silent → 「健康沒否決」
   與「靜默退化」無法區分。修 `j1_with_veto` 確認路徑也編 `j2_ran(chat,conf):reason` 足跡；
   腳本加 j2_executed/veto/failsafe count。**重啟後新資料窗才有足跡**。
② ❌ **guard `empty_after_strip` 查證後不動**：4 條不一致 3 條是 5/27 修正前舊樣本，
   對現行 guard 重跑全部放行。差點照 9 天前死資料動沒壞的東西——`feedback_audit_data_purity`
   再次擋下幽靈修法（先 reproduce 再改）。
③ ⏸️ weak_play_curation threshold 不動（降會放大「始作俑者」成語假陽性）。
報告：`records/judge_outcomes_analysis_2026-06-03.md`。

## 5/27 改動 — 全部 deployed

| 編號 | 任務 | Commit | 狀態 |
|---|---|---|---|
| A | Race: guard 不直接 commit（new `fast_path_excludes` 參數）| `8960a67` | ✅ |
| B | J1 threshold 0.90 → 0.85 | `8960a67` | ✅ |
| E1 | volume_agent（mute / down / up） | `2244eee` | ✅ |
| E2 | replay_agent（重播 / 再放一次 / 倒回） | `1cf2b20` | ✅ |
| E3 | now_playing_agent（現在播的是什麼） | `a6a6772` | ✅ |
| **J2-1** | **ChatClassifierJudge 純函數** | `081c11c` | ✅ |
| **J2-2** | **J1+J2 veto wrapper（j1_with_veto.py）** | `6c065f4` | ✅ |
| **J2-3** | **Groq adapter（TieredLLMRouter → call）** | `e62c537` | ✅ |
| **J2-4** | **Wire 進 shadow + MARVIN_SHADOW_J2_ENABLED=true** | `ed7a813` | ✅ |
| C | playback_control skip 加 chat prefix filter | `a57c579` | ✅ |
| D | music_agent_v2 weak_play_specific 非音樂後綴 blocklist | `bd2cf28` | ✅ |
| F | 裸歌名 fallback（cleaned-only） | — | ⏸️ deferred 6/1 |

J2 設計轉向（5/27 中段）：原 rewriter 角色被 J3 cleaner 覆蓋，改成 **chat veto**
（議題 E 後新發現的 Type 3 dead zone：「像意圖但實際是閒聊」case L19、L32、L48
J1/J2(舊)/J3 三 judges 都會誤判）。

## .env flag 已開

```
MARVIN_SHADOW_J2_ENABLED=true
```

需要 `launchctl kickstart -k gui/501/com.marvin.discord-bot` 重啟 bot 生效。

## 6/1 評估清單（替代原 6/3）

### 1. 量化指標（跑 `python scripts/analyze_judge_outcomes.py`）

| 指標 | 5/27 baseline | 6/1 目標 | 不達時 |
|---|---|---|---|
| 樣本數 | 49 | ≥80（5 天 ~30/天）| 視窗延一週 |
| **J1 fast-path 率** | 38.8% | **≥50%** | threshold+guard 沒生效，回查 |
| **J1/J3 agent 一致率** | 87.9% | **≥92%** | 不切 authoritative，做 Task F |
| **J2 unique-win 率** | — | **≥10%** | J2 對 J1+J3 都 miss 的 case 命中比例。<5% → 拔掉 J2 |
| Race p95 latency | 22ms | ≤500ms | 超過代表 J2 LLM 太慢 |
| 新 agent 觸發 | 0 | volume/replay/now_playing 各 ≥3 次 | 數據不可靠 |

### 2. 重點 case 檢查（人工挑 jsonl）

- L19「應該下一首就是」style → 應該被 J1 chat-prefix filter 擋下 (Task C)
- L48「幫我找…網站」style → 應該被 J1 weak_play_specific blocklist 擋下 (Task D)
- music/playback_control intent → 看 `vetoed_by_chat` reason 出現頻率（J2 是否實際發揮作用）

### 3. 早停條件（依算力預算 6/7 結束）

**Positive (任務完成，6/1 之前達標)**：
- 上表 5 指標全綠 + 觀察至少 3 天 → 宣告「Tier 1 完成」，6/2 起轉做 tier-2 authoritative 規劃
- J2 unique-win <5% 連續 2 天 → 拔 J2，省 Groq 成本

**Negative (止損)**：
- race 例外率 >2% 或 p95 >800ms → 停 J2，回 2-judge shadow
- 一致率反而崩到 <85% → 新 agent / 既有 agent 衝突，停下調 schema
- 6/3 前 J2 接線沒完成 → 砍 J2，只做新 agent

## Task F 仍 deferred 的決策依據

- L37 1/49 樣本（2%）— 實際 winner 已是 J1 0.85，**用戶體感正確**
- F 只是讓 J3 也命中 → agreement +2% → 87.9% → 89.9%，仍未過 90%
- C+D+E+J2 五層改善後預期自然過門檻
- F 跨 IntentContext + cleaner_judge + music agent 改 source_path，FP 風險高
- 6/1 數據看 agreement 卡在 89% 且 L37-style 是 gap 時再做

## 樣本平衡風險

5/27 49 條多數來自 5/26 一晚的 26 條。6/1 重跑時：
- 確認跨日分佈（避免單晚偏差）
- 對「J1/J3 一致率」誤差容忍 ±5%

## judge_outcomes_analysis_followup
*shadow race 上線後三天回來跑離線分析，看 J1 hit rate / latency / fallback rate*

**待辦（2026-05-27）**：shadow judges race 已於 2026-05-24 上線（commit `7b866ef`），
fire-and-forget 寫到 `records/judge_outcomes.jsonl`。預計收三天資料後回來做離線
分析，決定下一步。

**要算的指標**：
1. **J1 hit rate** — winning_judge="j1_regex" 的比例（命中即 fast-path，省 cleaner 一輪）
2. **J1 vs J3 一致率** — 兩個 judge 都完成時，winner.name 是否相同（驗證 J1 正確性）
3. **各 judge p50 / p95 latency** — outcomes[].latency_ms 統計
4. **Fallback rate** — winning_judge=None（dense zero）的比例 → IntentBus 也接不到的比率
5. **Per-agent 命中分布** — winner_name 的 histogram（哪些 agent 最常被 race 命中）

**Why:** 沒這些數據就不能判斷該不該開 authoritative mode（J1 命中跳過 cleaner）、
是否要加 J2（Groq 8B rewriter）、或先做 J1 schema 挖掘改善迴圈。

**How to apply:**
- 2026-05-27 開新 session 時提這個 memory
- 先 `wc -l records/judge_outcomes.jsonl` 看樣本量：
  - **≥50 條** → 寫 `scripts/analyze_judge_outcomes.py`（pandas / jq）跑量化分析
  - **<50 條（預估會走這條，每晚 ~6 條，3 晚 ~18 條）** → **人工逐條判讀**：
    把 jsonl 印出來人眼看 J1 winner 對不對、J1/J3 不一致的 case 屬於哪種、
    哪些 raw_query 兩 judges 都 miss。質性結論 + 樣本人工標註優於勉強統計
- 把結果 + 建議寫進 follow-up memory，決定下一階段：
  - J1 hit rate 高（>30%）+ J1/J3 一致率 ≥90% → 規劃 J3 authoritative mode 切換
    （要做的事見 `speculative_stt_pipeline.md` 的「J3 ClenerJudge 待辦」段）
  - J1/J3 不一致率高（>15%）→ 先做 J1 改善迴圈（見 `j1_improvement_loop.md`）
  - 兩者都不夠 → 接 J2 Groq 8B rewriter
    （要做的事見 `speculative_stt_pipeline.md` 的「J2 SmallLLMJudge 待辦」段，
    含 model 選擇 / prompt design / adapter / threshold 校準七步）

**前置觀測（2026-05-25 確認）**：
- shadow 寫入正常運作，2026-05-24 22:38~23:04 共 6 條紀錄
- 每晚活躍時段 ~30 分鐘，~6 條/晚 → 5/27 預估累積 ~18 條
- **5/27 走人工判讀路徑為基準假設**；不夠就延到 6/3 但仍先做質性分析

**✅ 2026-05-27 已完成**：報告在 `records/judge_outcomes_analysis_2026-05-27.md`，
量化腳本在 `scripts/analyze_judge_outcomes.py`，所有 7 個議題已人工判讀，下一步見
`judge_followup_actions_2026-05-27.md`。本 memory 保留作歷史參考；下次分析 6/3 跑同一腳本。

**2026-05-26 中期掃描（20 條 = 6 + 14）發現的重點 case**：
- **line 2（5/24）**「麻煩你問你好嗎馬文你好嗎」：J1=guard `empty_after_strip:'麻煩'`
  攔下；J3 跑 cleaner 後 dense zero。看 cleaner 怎麼處理這種 wake-spam 是對是錯
- **line 13（5/25）**「麻煩找歌麻煩找歌詞麻煩找歌詞天青蛇等煙雨在這裡」：J1=guard
  `empty_after_strip:'艾'` 攔下；**J3 看 cleaned 文字 → find_song 0.9 命中**。
  這是 cleaner 從 STT 幻覺救出真意圖的活範例 —— 5/27 重點研究：guard 是不是太
  aggressive、要不要讓 J3 在 J1=guard 時依然走 cleaned 路徑（race 規則層面）
- **`weak_play_curation` (0.85) 系統性撞 J1 threshold 0.90 不過**：所有 artist-only
  query 都讓 J3 拿 winner，但兩邊 agent 一致。考慮 5/27 提案：
  J1 threshold 降到 **0.85** 讓 curation 也 fast-path，省 J3 開銷
- **J1 fast-path 率**：13/20 = 65%（排除 3 條 dense zero 後 76%）
- **J1 vs J3 agent 一致率**：18/20 = 90%
- **Race latency**：全部 <10ms（中位數 ~1ms），shadow 路徑無感

5/27 開分析時這三個重點 case 必須優先看，不要被「跑統計」拉走注意力。

## llm_paid_pool_wrong_key_bug
*dailyreview 報 Gemini monthly spending cap 但 paid key 明明有額度時，先查 build_paid_review_pool 的 key 優先序*

2026-06-04 incident：dailyreview（6/2、6/3）連兩天三次嘗試全失敗，log 報 `429 RESOURCE_EXHAUSTED: monthly spending cap`。但 user 確認 paid key 還有額度、AI Studio spend cap 沒問題 → **不是配額問題，是 bus 抓錯 key**。

根因：`llm_pool.build_paid_review_pool()` 的 key 解析原本只 `env.get("GOOGLE_API_KEY") or env.get("GEMINI_API_KEY")`，**漏讀 `GEMINI_PAID_API_KEY`**。所以名為「付費 review 池」實際拿 free key 認證。

**2026-06-04 全庫稽核 + live 探測校正了 spend cap 歸因**（前一版這裡寫錯成「free spend cap=0」）：
- 兩把 key 角色：`GEMINI_PAID_API_KEY`(sha8 `36f5fce6`)=付費 project（開帳務，YouTube Data API 也掛這個 project，track_quality 用它當 YT key）；`GOOGLE_API_KEY`(sha8 `3c2dc4c4`)=**純 free tier，無 spend cap**。
- **判別法**：只有開帳務 project 會回 `monthly spending cap`；純 free 只回 `quota/429`（free key 的 gemini-2.0 系列就是回 quota 實證，per-model 配額各自獨立）。
- 所以歷史 spend cap **一定來自 paid project**：≤6/1 daily review 用對 paid key 但 67k batch 灌爆 cap；6/2 改走 bus 後 bug 切到 free key→不再 cap 但 JSON 截斷+`_today_str` UnboundLocalError；今天還原 paid key（符合「大 batch 走付費」拍板）+ 修 _today_str，live ✅。
- 稽核結論：**全庫只有 build_paid_review_pool 這一處用錯 key**，其餘 paid/free 路徑（router free→paid fallback 含成本守門、game/music/cleaner 走 free）全部正確。

諷刺點：`scripts/analyze_daily_log.py` 自己那行 GOOGLE_API_KEY 區域變數**有**正確優先 GEMINI_PAID_API_KEY，但 6/2 改走 bus 後那個區域變數變死碼，bus 自己重讀 env 只看 GOOGLE_API_KEY。這是「caller 算對了 key 但 bus 不吃」的 layering 陷阱，延續 `feedback_llm_calls_must_use_bus` 那條債。

修正：key 優先序改 `GEMINI_PAID_API_KEY → GOOGLE_API_KEY → GEMINI_API_KEY`（llm_pool.py build_paid_review_pool）。測試 `tests/test_paid_review_pool.py::test_pool_prefers_paid_key_over_free_google_key`。

**診斷捷徑**：dailyreview/任何 call_paid_review 報 spending cap 但 user 說 paid 有額度 → 別急著查配額，先確認 bus 解析到哪把 key（用 client_factory 攔截比對 sha8，不印明文）。關聯 `feedback_llm_bus_model_staleness`（同樣是 bus silent failure 家族）。

## project_devlog_content_roadmap
*build-in-public 內容策略——Q&A 格式、雙軌(X英文技術/Threads中文故事)、發文頻率、題庫*

**目標（2026-06-05 定）**：把 Marvin 開發過程分享出去，**幫同路開發者省時間**——不是追 star/爆紅（那是樂透+錯燃料）。價值＝「有人此刻在 Google 你卡過的問題，找到你的答案」。搜尋驅動的複利，慢但穩。動機背景見 `project_history_simon_suki_marvin`。

## 格式規則：Q&A（強制）
**每個 thread 第一篇 = 問題是什麼 + 怎麼解的。** 用「別人會搜尋的那句話」當開頭，同時是鉤子又是搜尋落地頁。後面幾篇才展開細節。已驗證：DAVE thread 第一篇就是「bot 加入語音卻轉錄不出東西」→ 立刻給方向。**單位是「一個有人在搜的具體問題被解掉」，不是「我專案的故事」。**

## 核心機制：一份工作餵兩個受眾（別重工）
踩到/解掉一個地雷 → 寫一篇 `docs/*.md`（永久家、會被搜到）→ 拆成：
- **X：英文技術 thread**（全球同路 dev，連回 docs）
- **Threads：中文故事/翻車/為什麼這樣設計**（在地、人味）
同題目 X 走硬核、Threads 走軟版——換角度不是翻譯，寫一次兩邊發。

## 頻率（可持續 > 頻繁；真實開發驅動，別排死表）
- **X**：英文技術 evergreen，每 ~2 週 1 篇（docs 那篇本身就是工）
- **Threads**：中文 build-log，每週 1–2 篇（工作副產品）
- 零成本習慣：每次修難搞東西順手記三句「問題／為什麼難／怎麼解」＝素材庫
- 現在是「深化期」地雷變少，別逼週更技術會燒乾。第一個有意義回應 > 發文數。

## 字數上限（已用程式驗證）
- **X 免費 = 280**；URL 不管多長算 23（t.co）；**中文 1 字算 2** → 純中文實際 ~140 字/篇
- **Threads = 500**（URL 算全長）→ 同內容可併成更少篇
- 已發的 DAVE：X 9 篇(全≤247)、Threads 5 篇(全≤446)

## 題庫（X 技術 thread，照「多少人撞×多難找」排）
1. ✅ DAVE 牆（已發 2026-06-05；docs/DISCORD_VOICE_STT_ON_MACOS.md）見 `voice_pipeline_dave_to_stt`
2. TTS 選型 → 為什麼 Edge TTS（像人聲+近免費）；素材 tts_engine.py
3. LLM 成本治理 → 一晚燒 100 塊 → bus + 急迫性分流；見 `feedback_llm_calls_must_use_bus`
4. 零鍵盤語音 UX → wake word + TTS 當整個 UI
5. per-person 記憶/DNA → 不是一個 prompt 打天下
6. IntentBus pattern → 加意圖不動 if/elif
7. VAD 自適應噪音地板 + 對話溫度
8. Plan 12 本地 f32 混音台（跑穩再寫；見 `project_plan12_local_mixing`）
→ 每篇配一個 Threads 中文軟版（#3=「Suki 那晚燒我 100 塊」、#2=「在一堆機器人聲音裡繞多久」）。8 題×雙週≈穩發一季。

## 平台分工（已定）
- **X 發英文**、**Threads 發中文**。
- 已發：Marvin 起源故事（中文 Threads + 作品集頁 marvin-story.html on GitHub Pages）、DAVE 技術串（X英/Threads中）。

## 評論處理
我**無法自動監看** X/Threads（不會被通知、登入牆+反爬蟲、只在被叫時才動）。流程＝**使用者把回覆原文貼給我，我草回覆**。偶爾可給我公開貼文 URL 試讀，但 X 常擋、別依賴。

## project_gap_research_wedge
*免喚醒資訊真空偵測+靜默交付的進度、領域不匹配發現、Phase 2 gating 條件*

主動型 AI 願景的功能 1（資訊真空偵測）+ 功能 2（靜默交付）。Plan 見 docs/plan_gap_research_wedge.md。

**已上線（2026-06-02，shadow，env 預設 off）：**
- gap_research.py：pre-gate（has_uncertainty_signal 規則 + should_escalate cooldown）→ UncertaintyDetector（綁 router.quick，注入 _shared_tier_router，享 bus 兜底）→ ResearchAgent / SilentDelivery（standalone，Phase 2 用）。
- 串接 voice_controller.handle_stt_result 的 debounced 鉤點（每句 finalized utterance，事件驅動非輪詢）。`GAP_RESEARCH_MODE` 未設=off=零開銷；shadow 只寫 records/gap_research.jsonl 不交付。
- analyze_gap_research.py 掛 3am batch。replay_gap_research.py 離線量測。

**Why Phase 2（真 research lookup + 真 delivery sink）被 gate 住：**
- 離線 replay 揭露**領域不匹配**：Marvin 真實語料是朋友胡鬧閒聊，真‧事實研究缺口 ≈ 0。寬鬆 prompt 會把 banter（「混凝土拌義大利麵」）塞成垃圾 query（n=50→5 garbage）。**收緊 prompt 後假陽性歸零**（n=40→0），但真命中也 ≈0。
- 同一結論第二次出現（因果圖譜可行性也是領域不匹配）。

**🟢 shadow 已於 2026-06-02 開啟**（.env `GAP_RESEARCH_MODE=shadow`，bot 重啟驗證 `[GapResearch] mode=shadow` log 出現）。**約 2026-06-09 回收一週數據**做 Phase 2 裁決。

**How to apply：** 要不要建 Phase 2 交付，**取決於 shadow 一週的 live 數據**——golden 語料偏閒聊、可能低估聊裝備/技術時的真缺口。回收法：看 records/gap_research.jsonl 的 hit 數+query 品質（3am `analyze_gap_research.py` 自動日報）。有真缺口→建 Phase 2（真 research lookup + SilentDelivery 接 bridge/文字頻道）；近零→擱置，轉做合領域的「軟版時序聯想」（suki callback/highlight）。精準度已驗證 OK，不會 spam。關掉：.env 移除該行或設 off + 重啟。

## project_history_simon_suki_marvin
*Marvin 真實起源與功能時間線（git 看不出來，git floor 是開源日 2026-05-07 騙人）*

這隻 bot 真實只有 ~3 個月大，**不是** git 看起來的樣子。命名演化 Simon → Suki → Marvin 壓縮在 2026-03 ~ 4 月中、約六週。

**⚠️ 定年 pitfall（我犯過一次）**：`suki_memory.json` 的 `emotional_highlights[].timestamp` 是 LLM 生成時掰的，飄到 2024，**不可信**。真實時間要看 `records/daily/*.log`（檔名即日期、行首是真 STT 時戳）+ git。我第一次查信了 suki_memory 結論「2 年」是錯的。

**起源（證據釘死）**：
- **Simon ≈ 2026-03**：唯一錨點是 Jack 跟 Showay 的 WhatsApp（沒進任何 log）。log 只抓到它臨終：2026-04-30 [showay]「原本 Simon 都快要生命結束就被你搞一個新東西」。化石＝venv 至今叫 `venv_simon/`。
- **Suki ≈ 4 月初，極短命**：records 最早真資料 `suki_golden_dataset.jsonl` = 2026-04-04 16:50（system prompt「Suki 的私人偏見」）；群友 6/3 回憶「4 月 3 號還在 Suki」「Suki 做一堆小段時間而已」「Simon 完然後換 Suki」。Suki 是長出記憶/人格的年代→整套系統永久以她命名（suki_memory/miner/budget、DB 欄 suki_impression）。
- **Marvin ≈ 4 月中至今**：2026-04-26 群友已叫「馬文」；init 字串自最早 log（2026-04-25）即 `Marvin Bot (Cog Edition) Initialized`。厭世人格(名字取自銀河便車指南)第一天就在。**2026-05-07 以 marvin-voice-core 開源**，git 從此起算，開源首月 412 commits。

**功能起點**：
- 📸 Vision 看螢幕截圖 — ~4 月底（最老，2026-04-25 log「截圖功能恢復了」、04-26「馬文看一下你的畫面」），開源時在 core。
- 🎵 音樂/YouTube Music — ~4 月底（04-28「你就一直放歌」），開源時在 core；6 月深做（無限續歌三層/品味 profile/per-song loudnorm，見 `project_infinite_autopilot_tiers`、`audio_per_song_loudnorm`）。
- 🎮 Busted 猜謎 — 2026-05-11（commit 390be20）。
- 📱 Companion app（marvin-voice-companion，獨立 app，bot 發事件過去）— 2026-05-12~14（companion_bridge/radar）。
- 🎯 Busted99 — 2026-05-14~17（引擎 8abd231）。
- 📺 Twitch 直播主支援 — 5/13 起、主浪 5/17（sub gifts/cheers/bits/badges）。
- 6 月＝深化期：自發漫才/打岔（`project_spontaneous_manzai`）、Plan12 本地混音台（`project_plan12_local_mixing`）、LLM 池治理（`project_llm_pool_attribution`）。

**作品集 HTML**：`/Users/jackhuang/Code/Discord-voice-bot/marvin-story.html`（self-contained，這故事的視覺化；repo 根目錄、未追蹤）。

## project_infinite_autopilot_tiers
*autopilot 佇列空補位的擴充策略——T1 團體記憶 / T2 發現(待做) / T3 回收(已上)，skip 鐵則*

佇列空自動補位（`voice_controller._auto_recommend`，一次 `_round_size=3` 首）的候選池擴充採三層，由窄到廣，枯竭才往下掉：

- **T1 團體記憶**（現有）：`music_recommender.build_recommendation_pool` 三 lane（group_resonance / long_tail / spotlight），這群人愛過的。
- **T2 發現**（**已上線**：PoC PR #12 + 接線 PR #14 + seed 改進 PR #19 + **多 seed 混合 PR #25**，2026-06-04）：`_t2_discovery_candidates` 從**單 seed 升級成點播史聚合多 seed 混合**（反映群組整體口味而非一首歌）。seed 池 = ①使用者最近手動點(`_last_user_song_seed`,優先,佔混合前段) → ②**點播史真人點過的**(`get_played_seed_ids`,**排除「Marvin推薦」自薦避免回音室**,按點播次數加權,每輪輪播窗口起點) → ③`get_liked_video_ids` 補。取前 `_N_SEEDS=3` seed 各跑 `ytmusic_radio` → `blend_radio_results` round-robin 交錯混合+跨seed去重(url/title)+exclude+limit（純函式,在 ytmusic_radio.py）。單 seed 失敗只跳過、全空才退 T3。→ `Candidate(lane="discovery", mode="direct", direct_url=watch_url)`。enqueue loop 認 `direct_url` → `_resolve_yt_query` http 直解。blocking `get_watch_playlist` 走 `asyncio.to_thread`（3 call 背景補位延遲可接受）。需 `ytmusicapi==1.12.0`；可用呼叫 `get_watch_playlist(videoId=X, playlistId="RDAMVM"+X)`（只給 videoId+radio 撞 KeyError 'endpoint'）。**T2 第三種 seed 源——LLM 品味鄰近（PR #26，2026-06-04，env-gated `LLM_TASTE_T2=on`，預設 off）**：破回音室。`taste_profile.py` + `scripts/build_taste_profiles.py`（每日離線跑、走 bus `call_paid_review`）讀每人 liked/played 歌 → LLM 推**史外鄰近歌手** + **負空間 avoid_artists** → `resolve_artist_seeds` ytmusic search 解析成真 videoId（resolve-then-trust 防幻覺）→ 寫 `records/taste_profiles.json`(gitignored, ts 帶新鮮度)。T2 runtime 只讀快取（`fresh_seed_ids` 進 seed 池與 history 交錯確保 novelty / `fresh_avoid_artists`+`filter_avoided` 排除 radio 候選），**語音熱路徑不打 LLM**。**狀態（2026-06-04）：已啟用觀察中**——`.env` 設 `LLM_TASTE_T2=on`、bot 已重啟、快取手動生成 4 位。**cron 未載**（launchctl bootstrap 被 Claude Code 安全分類器擋；plist 已建在 `~/Library/LaunchAgents/com.antigravity.marvin.tasteprofile.plist`，使用者要時自己 `launchctl bootstrap gui/$(id -u) <plist>`）。cron 沒載反而利於觀察（快取固定=單一變數）。**觀察重點**：`grep "T2 discovery"/"T2 avoid 排除"/"skip" bot_main.log`；好=鄰近歌手被聽完破回音室、壞=avoid 砍掉愛歌 or 鄰近歌一直被 skip(飄太遠)。下次 3am ritual 納入健檢；確認穩再載 cron + 視情況加 avoid cross-ref 安全閥。**已知待調**：avoid 跨在場成員聯集可能濾掉另一人愛的歌手（cross-ref 安全閥待加）；avoid 字串帶「(早期搖滾)」等限定詞使 substring 比對偏保守（少誤殺但也少作用）。是 `triadic_expert_pattern_domain_and_timing` 的活用：LLM=離線 biased expert（正向 seed + 負空間 avoid），radio=runtime 落地。

**未做的變化來源**：歌手精選歌單探勘（標題抽歌手→`search(filter="playlists")` 實測撈得到官方精選,uploader 欄是唱片公司不可用)。`get_charts(country="TW")` 未登入無 songs,不值得接。
- **T3 回收**（**已上線 PR #11**）：嚴格 exclude 掏空時放寬到「只保留 skipped 永久排除」，重播非 skipped 老歌。離線/API 掛掉的保命底，串流永不空轉。

**鐵則（每層都套同一組安全閘）**：`get_skipped_titles` 排除（skip 是 **skipper 的負訊號**，不代表點的人不喜歡，但 exclude skipped 是團體時間安全播法，Jack 拍板保留全排除）、已播去重 ring、`has_music_signal`（`iba_t0_wakeless_music` Gap B）、track_quality。

**歸因關鍵**：擴充靠**正向訊號**（在場者 liked / 點過沒被 skip）當 seed，**不**用 skipped 當 seed（避免往被嫌方向擴）。可選精細版「skipper 在場才排除該歌」未採（保守全排除優先）。

關聯 `skip_signal_attribution`（被 skip 調推薦邏輯不 blacklist 歌曲；exclude 自動推薦 ≠ blacklist 手動點，手動仍可播）。

## project_intent_gap_phase_a5_clustering
*Daily ritual 跑的 LLM batch clustering，把孤兒 intent_type 字串合併成 cluster；門檻 2 次升級*

Phase A.5 待辦：在 daily ritual 加 clustering pass。Phase A 只記原始 `intent_type` 字串（LLM 給的不穩定 — 同類意圖兩次可能寫成 `replay_user_history` 跟 `play_user_past_songs`），需要 batch clustering 才能算累計次數。

**做什麼：**
1. 讀當天 `records/agent_gaps.jsonl`
2. 對所有新出現的 `intent_type` 跑 LLM batch judge：「這幾個分別屬於 existing cluster 哪個？或開新？」（用 LLM 不用 embedding — intent 描述短，embedding 對短文易誤判）
3. 維護 `records/intent_clusters.json`（當前狀態檔，可覆寫）：
   ```json
   {
     "replay_user_history": {
       "members": ["replay_user_history", "play_user_past_songs", ...],
       "occurrence_count": 5,
       "first_seen": "2026-05-27",
       "last_seen": "2026-06-02",
       "status": "pending" | "ready_to_implement" | "agent_written"
     }
   }
   ```
4. `occurrence_count >= 2` → 升 `status="ready_to_implement"`（見 `feedback_intent_gap_threshold.md`）
5. Claude Code 下次 session 看 `status="ready_to_implement"` 清單補 agent

**何時啟動：** `agent_gaps.jsonl` 累積 ≥5 筆 **non-UNKNOWN** record 後值得跑（UNKNOWN 是 classifier 對「無意圖雜訊」的合法輸出，clustering 對它永遠是空 cluster）。

⚠️ **2026-05-28 修正**：原觸發是「總筆數 ≥5」，當天累積 5 筆但全 UNKNOWN，假性觸發 Plan 4。檢查 5 筆 raw_query 全是閒聊/反問。修法：`scripts/check_plan_triggers.py::check_intent_gap_clustering` 改成只計 `intent_type != "UNKNOWN"`，tests/test_check_plan_triggers.py 5 條鎖住。

**Why:** 沒 clustering，「滿 2 次就寫」門檻形同虛設（字串永遠不會完全相同 2 次）。clustering 是門檻機制的前提。

**How to apply:**
- 下次 session 如果看到 `records/agent_gaps.jsonl` 已累積資料且使用者問「該補哪些 agent」/「daily ritual 怎麼看 gap」/ 跑 daily_feedback_ritual → 主動提 Phase A.5 該動手
- 接線：daily ritual 是現存節奏（見 `daily_feedback_ritual.md`），clustering 是這條 ritual 的新分支，不是新獨立 script
- 寫測試對齊 Phase A TDD 慣例：先寫 `test_clustering_*.py` 失敗測試再實作
- `intent_clusters.json` 是**狀態檔**（可覆寫），不是 append-only 紀錄 — 跟 `agent_gaps.jsonl` 分開維護

## project_intent_gap_pipeline
*2026-05-27 上線的 agent gap 偵測 + 模板 ack 流水線；Marvin 之前的 cheap classifier*

Phase A intent gap detection pipeline 2026-05-27 上線（`intent_gap.py` + `voice_controller.py` 接線）。

**架構（4 元件）**：
1. `IntentGapRecord` — `records/agent_gaps.jsonl` 單筆 schema（`schema_version=1` 從第一筆算起）
2. `IntentBus.build_intent_manifest()` — 每日 cache 的 agent 能力地圖（DeclarativeIntentAgent only）
3. `make_groq_gap_classifier(router)` — 沿用 `self._shared_tier_router` 的 quick tier，回 `{intent_type, slots, nearest_agent, nearest_distance, ack_text}`
4. `handle_intent_gap()` orchestrator — 寫 JSONL + 5min dedup + 條件式 ack TTS，回 `IntentGapRecord` 讓 caller 判 fall-through

**接線位置**：`cogs/voice_controller.py` 3937 之後（`has_intent_signal` 過完）、Marvin 主 LLM 之前。`intent_type != UNKNOWN` → ack + `return` skip Marvin；`UNKNOWN` / classifier 例外 → fall through Marvin 兜底閒聊。

**Why:**
- 「Marvin 假承諾」是 prior 事故 pattern（5/23 蕭煌奇/下雨天的聲音幻覺）— gap path 把「沒實作的功能」明確 ack 出來，不再讓 Marvin 假裝「已為你播放 XX」
- 同時建 agent gap 資料供未來補 agent；使用者拍板門檻 2 次（見 `feedback_intent_gap_threshold.md`）
- Phase A 刻意不碰 NemoClaw / openclaw — tool calling 是 Phase C 的事

**How to apply:**
- 修改 `intent_agents/` 後不需手動更新 manifest — 每日 cache auto-invalidate（ISO date key）
- 新增 gap 相關 log 檔時對齊 `schema_version=1` 紀律
- Cheap classifier 沿用 `_shared_tier_router.quick(caller="gap_classifier", json=True, temperature=0.0)`；router 沒注入時 silent skip（測試 / 啟動失敗無回歸）
- ack TTS 由 LLM 同一次 call 順便產（自然口語、≤ 30 字、繁體中文）；5min dedup per `(speaker, intent_type)` in-memory，restart 重來無妨
- daily ritual clustering 機制**尚未實作**（Phase A 後續）；目前 `agent_gaps.jsonl` 只是原始紀錄

## project_intent_rescue_pipeline
*bus no-winner 時 LLM 改寫重投 + pragmatic signal 訊號回饋；env-gated 預設 OFF，shadow 預設 ON*

LLM rescue pipeline 上線於 2026-05-28（6 個 commit：3db977d → d869cef）。架構：bus.dispatch 找不到 winner（無 bid 或全部 < 0.30）時，呼叫注入的 LLMRescueAgent → 用 TieredLLMRouter.quick(json=True) 改寫成 regex 可命中的句子 → 帶 pragmatic_signal/target 重投 bus → emit 一筆 record 到 records/rescue_outcomes.jsonl 給 daily ritual mine。

**Why:** 中文語意常常 regex 抓不到（委婉、反諷、假正向「希望下次播放好聽的歌」字面正向實為對當前不滿），但 LLM 又比 regex 不可靠。設計目標：regex 處理 90% 文本，LLM 撿回剩下 10% 並讓那 10% 變成可學習訊號（convergent → 提案擴 regex；divergent → 餵推薦扣分；unmatched → 落回 agent_gaps）。

**How to apply:**
- 啟用：`MARVIN_INTENT_RESCUE_ENABLED=1`（預設 OFF 安全降級）
- 校準週後關 shadow 上線：`MARVIN_INTENT_RESCUE_SHADOW=0`（預設 ON）
- daily ritual 跑 `python scripts/analyze_rescue_outcomes.py` 看四分流；convergent cluster `count ≥ 2` 標 ready_to_propose（遵守 feedback_intent_gap_threshold.md 的 2 次門檻）
- **下一步等什麼**：shadow 收一週數據後，校準 LLM 改寫品質 → 翻 shadow=0 上線 → 才開始做 music_agent_v2 handler 內的 `pragmatic_signal == "negative"` 消化邏輯（emit feedback event 給推薦扣分）。**不要**在校準數據看完前就動 music agent 消化，否則 LLM 噪音直接打到推薦系統
- 程式入口：`intent_agents/rescue_classifier.py::build_rescue_components()` 是工廠；`intent_agents/llm_rescue_agent.py` 是純 logic；`intent_agents/rescue_outcome_logger.py` 是 sink

## project_judge_race_volume_2026-05-28
*Race coordinator 5/24 上線後 5 天樣本量遠低於 Plan 8 trigger「每天 ≥30」門檻，可能要重審*

Race coordinator 5/24 上線後實際樣本分布（含 5/28 partial）：

| 日期 | 樣本 | 備註 |
|---|---|---|
| 5/24 | 6 | 上線當天（commit 7b866ef） |
| 5/25 | 11 | |
| 5/26 | **26** | 高水位 |
| 5/27 | 13 | |
| 5/28 | 4 | partial（截至 01:14） |

5 天累計 60 筆，平均 12/天。最大 26 已逼近 30 但從未跨過。

**Why:** Plan 8 (J1 改善迴圈) trigger 寫「7 天每天 ≥30」=一週需 ≥210 race。當前流量需要 2.5× 才會自然觸發。Plan 1/2/3/9（6/1 重收 analyze_judge_outcomes 一致率 ≥92%）的統計 power 也受此樣本量限制——60 筆樣本算一致率信賴區間寬。

**How to apply:**
- 6/3 重收 analyze_judge_outcomes 跑完後，先看實際 confusion matrix 樣本量再決定 Plan 1/2/8/9 trigger 是不是要放寬
- 流量自然成長候選：J2 從 shadow 升 authoritative 會增加 race 觸發、voice channel 使用時數增加會增 wake 次數
- 不要為「讓 trigger 觸發」而放寬條件——trigger 太寬 → (a) confidence 校準 statistical power 不夠 → 校準誤差傷 J1 production
- 若 6/1 後流量仍低，比起改 trigger，更應該想「為什麼一天才 wake 10-20 次」（用量問題 vs detector 問題）

**2026-06-04 J2 驗證（離線跑 classifier）**：J2「exec 1/veto 0」一度懷疑設計錯，離線把真命令/明顯閒聊/可疑FP 餵 `make_groq_chat_classifier` → **8/8 全對**：真命令(播放楊宗緯/陳綺貞)放行(is_chat F 0.95)、含播放詞的閒聊(為什麼/不想/應該/好不好)正確 VETO(0.92-0.95，regex 分不出的歧義)。結論：**J2 設計沒錯、classifier 很準，留著當便宜安全網**（只在 J1 music/playback≥0.85 才打 Groq 8B ~150ms）。「exec 1」不是失敗，是 J1 自己的 modal/question guard 已先攔掉大部分 → J2 殘餘觸發少。**別追「unique-win ≥10%」門檻**（當初假設 J1 較差，現在 J1 太好所以達不到 = 好事，不是 J2 爛）。順手債：`analyze_judge_outcomes` 的 j1_false_positive 啟發式會把歌名(始作俑者)誤標 FP，製造假警報，低優先可修。關聯 `j1_improvement_loop`。

## project_llm_pool_attribution
*"LLM 池歸因/分流三部曲（#1 purpose"*

2026-06-03 處理「LLM 踩冷卻特別快」，分三類修（commit caca126 / e2272fb / 0076803 / 7b16350）：

- **#1 purpose 自動歸因**：`_call_llm` 不傳 purpose → frame 取呼叫方 method 名。修掉 `llm_routing.jsonl` 全標 marvin_chat 的盲點。
- **#2 cleaner 無效 call**：截斷 JSON 救援（`recover_truncated_cleaner_json`）+ prompt 收緊 Siri→馬文 過矯正。
- **#3 急迫性硬分流**（d1751eb，取代原 −0.20 軟降權）：GroqAgent 對 `BACKGROUND_PURPOSES` 直接 decline 0.0（即使 Groq 閒置也 decline，保留每日 TPD 給即時）→ 逼去 Cerebras（近無限 RPM）/gemini-free/付費。免費池非單一：Groq 稀缺、Cerebras 充裕，分流到 Cerebras 即解 contention、零成本。**已驗證 #2 解決核心問題，off-peak 搬遷非必要**（有新鮮度代價，先看報表數據）。
- **社交分析空轉移除**（d1751eb）：社交補位關閉後 `analyze_social_dynamics`（社交知識圖譜，長上下文）結果唯一消費者也關 → 每 10min 算了丟。改成補位關閉時連 call 都不發（gate 在 `_SOCIAL_INTERVENTION_ENABLED`）。記憶萃取早已每日 off-peak。
- **3am 報表**：`scripts/analyze_llm_purpose_breakdown.py` → `records/llm_purpose_breakdown_<date>.md`，追 per-purpose 量 / cleaner 救援率 / 過矯正次數。

**下一步（等數據，別盲配）**：#3 的精準 per-purpose 導流需要 #1 上線後累積幾天的 labeled data。跑幾天後撈報表看：哪些背景 purpose 吃最多池、要不要把使用者可見生成器也納入背景集、救援率/過矯正是否改善。6/2 baseline = 救援率 0%、過矯正 22。

**已知 bypass 債（治本=#3 大重構，未做）**：reactive 主路徑 `stream_llm`（dedicated client）+ `profile_compressor`（直連 Groq）**繞過 bus、不記 log、不吃 cooldown**，卻打同一 Groq 帳號配額——這才是「背景在 bus 讓位、reactive 仍直捶 Groq」的根源。收進 bus 是動熱路徑的大工程，待獨立處理。相關：`feedback_llm_calls_must_use_bus` `feedback_llm_bus_model_staleness` `feedback_data_driven_diagnosis`

## project_plan12_local_mixing
*決定把串流播放核心改成本地 f32 混音（取代 hotswap second-stream）的方向、測試策略，以及 Marmo 的定位*

**⚠️ TTSScheduler 抽離計畫已腐爛（2026-06-04 標記，勿照舊藍圖實作）**：6/03 那份 `jackhuang-main-design-Plan12-Scheduler-20260603-085249.md`（ENG CLEARED, SCOPE_REDUCED, 待實作 T1-T4 全未打勾）在「先跑穩幾天」期間被 ~28 個 commit 推翻核心假設，讀碼驗證的三個腐爛點：① **mixer 已是雙層 TTS**——新增 `push_tts2`/`_tts2_queue`（打岔層 layer2/Marmo，`local_mixing_source.py:72-250`），`clear_tts` 一次清兩層；計畫只模型化單一 TTS FIFO。② **新增 `_tts_protected` 中斷政策（中斷政策二元化）**——`voice_controller.py:496/2548` 的 `if is_playing_audio and not _tts_protected` 才是現在能否打斷的真正閘；計畫 interrupt 決策定於此政策之前。③ **所有 file:line 錨點全漂**（push_tts 現 :750+:3690、`_tts_interrupted` 現 :495/1937/2554/3961/4018/5929/6215/6280、interrupt :2554 非 :2464）；照舊行號表實作=踩計畫自列的 failure-mode #2 split-brain。已把 staleness banner 寫進該 doc 開頭。**開工前要先解的架構題**：TTSScheduler 邊界要不要涵蓋 layer2(打岔) 與 `_tts_protected`，或明文 layer1-only——解完再重跑 /plan-eng-review。決定（2026-06-04）：擱置不現在審。

**🔴 T5 第一次 live（2026-06-02）撞兩個真 bug → flag 回退 off，code 留 repo（commit cd46dda..3c68ba5, pushed）**：
1. **串流音樂斷續**：always-on `mixer.read()` 在 discord voice thread 上**同步讀 `FFmpegPCMAudio.read()`（網路串流 ffmpeg pipe）**，網路/ffmpeg 一 hiccup 就卡住整個 mix 輸出 → 斷續、最後斷掉。log 無 exception（read() 沒 raise），純 pacing 問題。修法：音樂層要**緩衝執行緒預讀 ffmpeg 進 queue**，把 mixer.read() 跟 ffmpeg pipe latency 解耦（pre-decode 對長串流不可行，要 streaming buffer）。這正是 outside-voice 警告的「nested 同步 read 在 voice thread」對音樂層成真。
2. **招呼語重疊**：summon 時招呼 + Marvin 登場兩段語音**同時播**。mixer 把 music 層 + TTS 層 overlay（或兩個來源同層）；舊 `playback_lock` 是**序列化**所有 play()。需要「該序列化的情境（兩段人聲）不要 overlay」的政策——不是所有東西都該混。
- 旁證：當次也順手把 stream 預設音量 10%→80%、按鈕步進 5%→10%（與語音 10% 一致），這兩個跟 flag 無關、已 commit 進 repo。
- **下次 attempt 重點**：先解音樂層緩衝（bug 1），再定 overlay-vs-serialize 政策（bug 2），才值得再開 flag。

**🔴 T5 第二輪（2026-06-02，bug1+bug2 修完後再開 flag）— 仍失敗、且難歸因 → 再回退 off**：
- bug2 修有效：intro duck 正常（commit f5685fa）。
- 但 flag=on 仍：① 音樂 5-8s 斷續復發 ② 18:29 一波 `CryptoError`（DAVE/SRTP 解密）風暴 → Sentinel 復原 → 掉連線（**既有 DAVE 脆弱性**，18:20/01:42 都有、與 flag 無關）③ wake 評分 0.266<0.32（跑了但低分，難判）④ summon greeting 後「使用說明」proactive TTS 沒出（疑上游 interrupt-guard/stream-mute drop，18:26:46 有 interrupt guard log，**非 mixer 佇列 bug**）。
- **關鍵**：mixer 整場**零 `[Plan12_Mixer]` error**（read() 沒 raise、沒 crash）。問題全是「mixer always-on 的重量 + 既有 DAVE 不穩 + 上游 TTS guard」糾纏，**log 無法乾淨歸因**。
- **教訓 / 下一步（別再盲打 live）**：
  1. **先加 instrumentation**（flag-gated）：mixer read() 耗時、underrun 次數/時長、buffer 深度、每秒 frame 數 → 下輪 live 才有數據判斷是 mixer 跟不上還是 DAVE/CPU。
  2. **重評 fork A vs B**：always-on（fork A）讓 voice send thread 持續逐幀 numpy（idle 也跑），跟 M1 8G + STT + DAVE 復原搶資源；**fork B（mixer 只在放音樂時跑、idle 不佔 voice thread）blast radius 小很多**，掉連線時也不必一直 re-arm。當初選 A 是為了「單一 play()、barrier 乾淨」，但 live 顯示 always-on 的成本被低估了。
  3. 已修的 commit（buffer/intro-duck/音量）是淨改善，保留在 repo。

**🟢 T5 第三輪（2026-06-02 晚，instrument 數據到手）— mixer 效能達標、瓶頸在 DAVE 不在 mixer**：
- **踩到的坑（重要）**：bot 把 `print()` 導到 **repo `/Users/jackhuang/Code/Discord-voice-bot/bot_stdout.log`**，不是 launchd plist 的 `~/Library/Logs/Marvin/bot_stdout.log`（後者只收 logging handler 輸出）。debug print 要看 repo 那個檔，浪費了好幾輪才發現。
- **數據結論（放歌 + unmute 期間）**：`[Plan12_Init] plan12=True mixer=True`；`[Plan12_Stats] read_ms avg=0.25ms / max<5ms / slow>18ms=0`、`buf=49/50` 幾乎全滿、`underrun` 從 9 沒再漲、`f=250-253/5s`=穩定 50fps。→ **「always-on 逐幀 numpy 在 M1 8G 太重」假設被推翻**：mixer 一路準時產好幀、buffer 沒餓、discord 正常 50fps 拿幀。
- **真因**：使用者鐵證「mic mute 沒事、unmute 馬上斷」。mixer 產幀正常但**送不出去**——卡在 **DAVE/SRTP 語音連線層**：unmute → incoming 封包 → 解密爆 `CryptoError` 風暴 → 連線 re-key/復原 → 連帶打斷 outgoing 音樂 transport。mixer 與此正交。
- **待定案**：always-on 持續送音是否「放大」DAVE 在 unmute 時的脆弱（vs 既有問題）。對照測試：flag=off 放歌 + 同樣 mute/unmute，比 CryptoError storm/斷續。若 flag=off 一樣 → 純既有 DAVE、Plan 12 mixer 可放心推進（只是要等 DAVE 層改善）；若只 flag=on 爆 → always-on transmit 放大 DAVE，要在連線層解。
- **重要翻案**：Plan 12 最大未知（mixer 在真機跟不跟得上）= **已驗證 OK**。剩下是 DAVE 層，largely 既有、與 fork A/B 無關。

**🔴🟢 A/B 定案（2026-06-02 晚）— always-on 送音是斷續主因，非既有 DAVE**：
- 對照：**flag=ON unmute→stream 馬上斷 + CryptoError 爆；flag=OFF（舊路徑）unmute→不斷、正常。** → 推翻「純既有 DAVE」假設。舊路徑放歌一樣送音卻沒事，差別在 **always-on mixer 那條「自連線起連續、永不結束的自訂 AudioSource」** 跟 DAVE/SRTP 收音的互動（疑：持續雙向 + DAVE MLS 金鑰輪替 → unmute 觸發 re-key → CryptoError storm → 連帶打斷 outgoing）。
- **fork A 的致命傷被定位**：always-on 連續送音（不是 mixer 效能、不是混音邏輯）。mixer read() 數據完美（<5ms/50fps/buffer 滿）。
- **下一步（offline 研究，非 live 盲打）**：① 查 discord.py 2.7.1 voice/DAVE 在「自訂 AudioSource 連續送 silence/音訊」時的 re-key 行為；② 重評 **fork B（mixer 只在放音樂時 arm、idle 不送）** —— 但注意對照測試是「放歌中 unmute」兩條都在送音、舊路徑沒事，所以關鍵可能是「自連線起連續」vs「每首歌 discrete play」而非「有沒有在送」；fork B 每首歌 discrete arm/release 較接近舊路徑、值得試；③ 或試「idle 時 mixer 暫停 source、有內容才 arm」把 always-on 降成 on-demand。
- **現況**：flag=off 穩定可用；Plan 12 code 全在 repo+pushed；debug 用的 [Plan12_Init]/[Plan12_Trace] print 已清，[Plan12_Stats] 真 instrumentation 保留。**print 要看 repo `/Users/jackhuang/Code/Discord-voice-bot/bot_stdout.log`，不是 ~/Library/Logs/Marvin/。**

**✅ on-demand 修好斷流（2026-06-02 晚，commit 待記）**：mixer 加 `on_demand` 模式——idle 超過 grace(1s) → read() 回 `b""` 讓 discord 停送；內容到達 caller 重 arm（每段播放=獨立 player thread，仿舊路徑 discrete play，消除「自連線起連續送音」因子）。cog: `LocalMixingAudioSource(on_demand=True)`；sentinel 只在非 idle 才 re-arm；_mixer_play_music 迴圈內 re-arm（重連安全）。**live 實測：unmute 不再斷流** → always-on×DAVE 假設證實+解掉。
- **新問題：音質「一直悶悶 distorted」**。stats 乾淨（read_ms~0.2ms、underrun 不漲、buf 滿）→ 非效能/underrun。離線 A/B 已證 f32-mix ≈ ffmpeg-baked ±2 LSB → **f32 混音本身不是兇手**。最可疑：① loudnorm 單遍 dynamic 模式的壓縮 artifact，**因預設音量 10%→80% 才變可聞**（舊路徑同樣 loudnorm，10% 時被小音量蓋住）——若是這個，flag=off@80% 也會悶（非 Plan12 專屬）；② s16→f32 經 FFmpegPCMAudio 的 roundtrip（outside voice #5 警告，但 full-scale s16→f32 理論無損）。**下一步：flag=off@80% 同首歌對照聽——也悶=loudnorm/音量(兩路徑共病)；乾淨=Plan12 專屬。**
- **✅ 對照定案（2026-06-02 晚）：flag=off@80% 一樣悶 → 悶是 pre-existing、跟 Plan 12 無關**。兇手是 `loudnorm=I=-14:TP=-1.5:LRA=11` 單遍 dynamic 壓縮 artifact（兩路徑串流 ffmpeg 都用），舊預設音量 10% 太小聲蓋住、80% 才現形。也可能 yt-dlp 選到低 bitrate 音訊源 or opus 編碼——pre-existing 音質 rabbit hole，獨立於 Plan 12。
- **Plan 12 狀態總結**：mixer 效能 OK + on-demand 修好 unmute 斷流 + intro duck OK + 音量即時 → **核心功能可用**。剩下的「悶」是既有音質問題（loudnorm/source bitrate），不是 Plan 12 的鍋。修法候選：loudnorm→dynaudnorm（streaming 友善、少 pumping）/ 減輕 loudnorm（低 I 寬 LRA）/ 查 yt-dlp format bitrate / 純 volume+alimiter。影響兩路徑、benefits 都拿。

**🎚️ 音質追查定案（2026-06-02 深夜，commit 67028d5 pushed）**：
- **悶的真兇 = 頻道 bitrate 64k（Discord 平台天花板，opus 被砍到 64k）+ loudnorm 單遍壓縮**。`[Plan12_Bitrate]` 印出頻道=64000。**解法非 code：把語音頻道 bitrate 調 96k（已調）/ 伺服器 boost 上 128/256/384**。opus 編碼器已改成自動拉到 `vc.channel.bitrate` 上限。
- **dynaudnorm 不能用**：`m=10` 自適應增益隨播放把安靜段越推越大 → **漸進破音（越播越 distorted）**。改回**無正規化**（純源音 + mixer 音量，使用者實測最乾淨）。要歌間響度一致 → 只能上「低 maxgain（m=2-3）」的溫和 dynaudnorm，別用預設。
- **yt-dlp 音源 OK**（bestaudio m4a/webm，非低 bitrate）。預設音量回 10%。
- **控制台按鈕 flag=on 失效根因 + 修**：按鈕控 `vc`（vc.stop_playing/pause）但音樂在 mixer 層，且 `_mixer_play_music` 迴圈的 reconnect re-arm 每 0.1s 把 stop/pause 抵消。修：skip/next/prev/jump → `mixer.clear_music()`；pause → 新增 `mixer.set_paused()`（adapter 續播靜音、不被 re-arm 抵消）；音量本來就有效（synced）。
- **此 session Plan 12 commits（push 到 4c14767）**：DSP module / mixer / adapter / 整合 / barrier / buffer / intro-duck / on-demand / instrumentation / opus-match / 按鈕 Plan12 化 / **TTS buffered-streaming / 打斷清佇列 / promo→TTS層 / backlog drop**。**flag 目前 on（測試中）**；穩定回退＝註解 run_bot.py:17 + kickstart。

**🎙️ TTS streaming + 打斷修復（2026-06-02 深夜，commit 4c14767 pushed）**：
- **TTS 改 buffered-streaming**：`_stream_tts_to_mixer` 邊收 edge-tts 邊 ffmpeg 解碼邊逐幀 push_tts（首音 ~0.8s，恢復舊 FIFO streaming 低延遲、解掉「greeting 貼到 intro 尾才出」）。render 在 event loop（readexactly 一幀）不阻塞 voice thread。pre-render(_render_tts_f32) 移除。**關鍵設計：FIFO/streaming 可用，只是不能裸接進 mixer（會阻塞 RT voice thread）；要嘛 pre-render、要嘛 streaming 在 loop 上逐幀 push（後者較好）。**
- **打斷累積 bug 修**：舊路徑打斷會 drop/flush，但 mixer TTS 佇列沒清 → 打斷的 TTS 殘留 + 不同時間 TTS 疊播。修：mixer.`clear_tts()`（打斷處理 2464 呼叫）+ streaming `_drain` 偵測 `_tts_interrupted` 停餵+kill ffmpeg + 新獨立發話解除打斷封鎖（否則 streaming 立刻 bail）。
- **backlog drop 重加**：play_tts flag=on 重加 priority drop（mixer.tts_load_seconds()>8/3s→丟+貼文），取代被 bypass 的 storm guard。
- **summon「使用說明」promo**：是語音 → 改走 TTS 層（序列在 greeting 後、全音量、不被 duck），不再走音樂層被 duck 成小聲。
- **控制台按鈕 Plan12 化**（67028d5）：skip/next/prev/jump→mixer.clear_music()；pause→mixer.set_paused()；音量 synced；label ±10%。

**🏗️ 下一階段方向（2026-06-02 拍定，先跑穩再做）— mixer 退回純機制 + 抽 TTS scheduler**：
- **問題**：現在 mixer 把「佇列 + cap + clear_tts + backlog drop + 打斷」這些**政策**焊進了**機制**層。這 session 的累積/打斷/backlog bug 就是症狀——政策放錯層，每個政策問題都得在 mixer 補一刀。
- **乾淨分層**：`IntentBus/SpeakBus（政策：講不講/講什麼/優先級/何時）→ TTS Scheduler（佇列/排序/優先搶播 preempt/打斷清除/滿了丟）→ Mixer（純機制：當前 music slot + TTS slot 即時混/duck/dither）→ Discord`。
- **mixer 該曝機制**：set_music_source / feed_tts(current source) / set_volume / set_duck / set_paused / clear / is_idle / tts_load。**不該擁有**佇列/排序/drop/打斷政策。
- **SpeakBus 不直接控 mixer**：透過 scheduler。SpeakBus 給「優先級 + 可否被打斷」，scheduler 翻譯成對 mixer 機制的編排（高優先 preempt=clear+feed；低優先+忙=drop；一般=排隊；音樂中=duck overlay）。
- **何時做**：等現版多跑幾天確認穩。**只要 SpeakBus 要做「優先搶播 / 某句一定 duck 不排 / 忙就丟」這類細控就值得抽**（現結構會越補越亂）。建議走 /plan-eng-review 過一輪再動。決定（2026-06-02）：先跑穩、不馬上重構。

**🆕 狀態（2026-06-02 更新）**：Plan 12 1-page sketch 完成（`~/.gstack/projects/butthead0819-beep-marvin-voice-core/jackhuang-main-design-Plan12-20260602-155144.md`）。**拍定 fork A = always-on source-level mixer**：bot 進語音就跑一條 mixer source（沒聲輸出靜音），所有 TTS/music/ack/radio/local 全餵進去 → 整個 session 只剩一條 `play()`、`playback_lock` 徹底退役、second-stream hotswap（hotswap_coordinator/hotswap_loudness/volume-swap）退役。ducking=source-level、preemption=duck+overlay 不用硬截斷。**連帶結論：voice_controller refactor 的 Phase 1 AudioPlaybackArbiter 取消**（arbiter 是為協調多條 play() 而設，單一 play() 後失去理由）。下一步見該 refactor plan：跑 Plan 12 自己的 eng-review（mixer frame 正確性/CPU/連續性，可用 Marmo token 起丟棄式 voice client 當測試載具）。

**狀態（2026-05-31 更新）**：離線 A/B 音質驗證**過關** ✅。腳本 `scripts/plan12_offline_ab.py`（10% / 30% volume，《煙與香水味，昨夜的雨》），ffmpeg 烤 vs f32 本地混音兩條路徑數值差 ±2 LSB（純 TPDF dither 噪音 ≈ -90dB），耳朵驗證無顯著差異。Plan 12 thesis 成立，可進入工程化階段。

**方向（2026-05-31 討論定案的「下一步」）**：把 Marvin 串流播放核心改成「本地混音台」——對 Discord 是一條不中斷的 `AudioSource.read()`，在本地逐幀（20ms）把音樂 + TTS + 音量混好。關鍵技術點：ffmpeg 解碼輸出 **f32 → 浮點做增益/混音/ducking → 最後 dither 成 s16le** 送 Discord，增益在量化前發生，等同「把 volume 烤進 ffmpeg」的音質卻能即時調。**這同時解掉當初逼我們走 second-stream hotswap 的低音量量化問題**；做成後 hotswap/coordinator/volume-swap 那整套可退役。

**Why**：目前音量/TTS 即時生效是靠 second-stream 熱切換（spin 第二條 ffmpeg + seek + 硬切，有接縫、2.5~4s lead）。本地混音台無接縫、當幀生效，是更正確的終局架構。但它是**播放核心的重寫**，風險高。

**How to apply**：
- **不要在 Marmo 上實作**。Marmo（=NemoClaw，openclaw 那隻）是無語音的文字 worker，做完事 POST 文字到 Marvin `/marmo-result`（marvin_voice_core/marmo_server.py），由 **Marvin** 用 MARMO_VOICE 講；Marmo 沒有 VoiceClient/播放路徑。混音台只能活在持有正式 voice 連線的 Marvin。
- **測試策略（使用者要先驗音質才肯動工）**：① 先寫**離線 A/B render 腳本**（純腳本、零碰 Marvin）——一首歌跑「現行 ffmpeg 烤音量」vs「f32 本地混音」兩個 WAV，耳朵 A/B，重點聽 10% 低音量段。② 音質過關才草擬 Plan 12，採 **feature flag 在 Marvin 內並行新舊播放路徑 + 灰度退回**（同 hotswap/J2 shadow 打法）。③ 若要驗 live 連續性/CPU，可用 Marmo 的 Discord token 起一個丟棄式 voice 測試 client（只有混音 source、無 STT/intent），但那只是測試載具，production 仍回 Marvin。

**Marmo 一搭一唱（另一條線，偏產品/體驗）**：使用者想讓 Marmo 從「只在完成複雜任務時才開口」變成能跟 Marvin 一搭一唱，作為專案特色。管線已通，缺的是「Marmo 何時主動插話 + turn-taking 不蓋台 + 雙人 persona」的觸發/內容層。與 Plan 12 互相成就（好混音讓 Marmo 插話好聽）但分開做。

**Marmo 設計現狀（2026-05-31 /office-hours + /plan-eng-review 雙審通過）**：design doc 在 `~/.gstack/projects/butthead0819-beep-marvin-voice-core/jackhuang-main-design-MarmoBanter-20260531-212422.md`（Status: APPROVED + ENG CLEARED）。

**核心決策（office-hours + plan-eng-review 雙審後最終版）**：
1. **角色 pattern**：Marvin = **boke / 跑題者**（厭世機器人，收問題後可進存在主義獨白），Marmo = **tsukkomi / 代用戶打斷者**（不是「嘴賤副嘴」這種形容詞角色，是功能角色——看到 Marvin 跑題立刻打斷站使用者立場給實用答案 + 反擊）。順序固定 `[Marvin, Marmo]` 由功能位差驅動
2. **架構**：1 class `DualSpeakAgent`（Template B 對齊 busted99 pattern）+ 1 module-level async function `generate_dual_dialogue()` + extend `IntentContext.payload: dict | None` + extend `dispatch_source` enum 加 `"marmo_inject"`。marmo_server 直接呼叫 `bus.dispatch(ctx)` fire-and-forget，**不新建 IntentBus.inject() API**
3. **Persona 系統重用**：Marmo 加進既有 `personality_config.CHARACTER_PRESETS`（axes: sarcasm 0.95 / directness 0.95 / compassion 0.15 / resignation 0.10），重用 `build_personality_prompt_context()`，**不新建 prompt 資產檔**
4. **Lock 邊界**：`playback_lock` 只包雙段 TTS 序列播放，LLM call + TTS gen 在 lock 外
5. **紅線 fallback**：post-gen filter 命中 → drop dual、原 marmo_text 走單 Marvin TTS
6. **Backpressure**：`DualSpeakAgent.bid()` 內讀 `tts_queue_duration > 10s` → 0.0 bid 防 storm

**Pre-PoC F1+F3 gate（寫 code 前必跑）**：用 LLM 生 5 組「Marvin 跑題 → Marmo 代用戶打斷」對白範例給朋友盲讀。**不能手寫**（測自己不測產品，見 feedback_mock_dont_self_fixture）。朋友 ≥3/5 想轉發 → PoC 走；朋友覺得「都是邪惡 Marvin」→ F3 反開先擴 axes。

**已解決的 Outside Voice tension**：F3（Marmo 是不是邪惡 Marvin）+ F4（LLM 自由順序 vs 固定 boke-tsukkomi）— 兩者都由 pattern 雛形「Marmo 是功能位差打斷者」解掉，不靠 axes 反義也不靠 prompt hardcode 順序。

**10x 願景**：mini-RLHF 觀眾養成（prompt 從 PoC 第一天就把 persona/attitude/temp/好感度做成 runtime 變數位）。

**PoC 第一步**：T1 = LLM 生 5 組 mock + 朋友盲讀（F1+F3 gate）。**朋友不笑就不寫 code**。過了才動 T3 `IntentContext` extend + Template B agent 等。

---

## 🚀 PoC LIVE (2026-06-01) — Marmo 一搭一唱 ship 完成

**現狀**：MARMO_DUAL_SPEAK=true 已上 prod。marmo-result POST → DualSpeakAgent winner=0.95 → Cerebras gpt-oss-120b (~700ms) → dual segments → Marvin (-20% rate / -15Hz pitch) → 0.3s pause → Marmo (+25% rate / +10Hz pitch)。性別 + 節奏 + 音高三重對比，使用者接受。

**Live 實測碰到的 6 個非預期問題（都修了）**：
1. **LLM Bus Cerebras 模型過期**：llama3.1-8b / qwen-3-235b 全 404。Cerebras 現在只剩 zai-glm-4.7 + gpt-oss-120b。改 `llm_pool.py:234` + .env `CEREBRAS_MODEL` 都改 `gpt-oss-120b`（zai-glm-4.7 是 reasoning model 回 reasoning 不回 content、不兼容 OpenAI 介面）。session_summarizer / marvin_chat 等其他 caller 一併受惠
2. **Reasoning model max_tokens 不夠**：gpt-oss-120b 吃 150-700 reasoning tokens 才開始輸出 content，預設 1024 不夠 → 改 `CerebrasAgent` 預設 max_tokens=2048 + 加 empty-content 診斷 log
3. **LLM Bus 不能 bypass（Jack 原則）**：第一版 wrapper 直連 `_call_cloud` 跳過 bus，Jack 否決——bus 是 shared infra，要修不要繞。改回 `_call_llm(tier=high, is_json=True, allow_local=False)` 對齊 gemini_router_content.py 慣例
4. **TTS Interrupt Guard 殘留**：上次 wake reply 被插話留下 `_tts_interrupted=True`，導致整個 dual 兩段都被 drop。修：`play_dual_dialogue` 開頭 reset flag（這是獨立 unit、不是串流續句）
5. **Marmo 用 neutral emotion 速度跟 Marvin 一樣慢**：兩段都 -20% rate 反差出不來。新增 `_EMOTION_TTS_PARAMS["marmo"]={rate:+25%, pitch:+10Hz}`，per-segment dispatch
6. **HTTP 401 unauthorized**：marmo-result 受 MARMO_TOKEN 保護，curl 測試要帶 X-Marmo-Token header（token 在 .env）

**Flip 機制**：`run_bot.py` 加 `os.environ.setdefault("MARMO_DUAL_SPEAK", "true")` + `launchctl kickstart -k gui/$UID/com.antigravity.marvin.bot`。回退把那行刪掉重啟即可。

**Live PoC 統計**：Cerebras 平均 latency 614-1241ms、HTTP fire-and-forget 1-12ms 返回、無 LLM error 也無 fallback trigger 的成功率 = 5/5（修完所有問題之後）。

## 🎭 兩種對白 pattern（2026-06-01 下午擴充，已 live）

dual speak 分兩個 case，由 `generate_dual_dialogue(pattern=...)` 參數切：

**Case A — Marmo 先說 / Marvin 吐槽（`pattern="marmo_lead"`，順序 `[marmo, marvin]`）**
- 來源：webhook POST `/marmo-result`（Marmo 主動有事報，由 DualSpeakAgent 接）
- Marmo 第一人稱報事（「我找到/我整理好...」）、Marvin 開頭點名 Marmo 吐槽 + 厭世感慨
- 一天頻率低（proactive alert / 任務完成回報）

**Case B — Marvin 先說 / Marmo 吐槽（`pattern="marvin_lead"`，順序 `[marvin, marmo]`）**
- 來源：`vc.speak(proactive=True)` 的主動發話 agent（BridgeAgent / MemoryCallbackAgent 已 migrate，ProactiveTopicAgent 還用舊 play_tts 沒接）
- 機率閘：`MARMO_DUAL_CHANCE`（run_bot.py，**現暫設 1.0 全開驗證**，太頻繁降回 0.5）
- Marvin 第一人稱跑題、Marmo 開頭點名打斷（「別聽 Marvin 的/ Marvin 你又在...」）給實際答案
- 一天頻率高（Marvin 主動講話機會 >> Marmo），這才是讓 dual 變「日常 vibe」的主力
- 機制在 `cogs/voice_controller.py::speak()` 內：proactive=True 才試（喚醒回應 proactive=False 走 single 不爆 latency），`_maybe_try_dual_upgrade()` 擲骰 + `_generate_dual_marvin_lead()` 生對白，失敗 fallback 單 Marvin

**互稱規則**（兩 pattern 都有）：反應的一方一定要在台詞裡叫出對方名字（Marvin / Marmo），讓對白像兩個有名字的角色對話而非旁白。

**對白品質演進（2026-06-01 下午，都 live）**：
- **漫才技法進 prompt**（網搜桂枝雀「緊張緩和」理論 + ツッコミ 公式）：Marvin=ボケ 壓沉重製造緊張 / Marmo=ツッコミ 短促打斷釋放；吐槽公式「你說『X』，他只是問 Y 欸」（複述＋點破）；認真接荒謬生二次笑點。實測 Cerebras 直接照公式跑出教科書級 ツッコミ。
- **人格定版**：**Marmo 刀子嘴豆腐心**（嘴賤外殼+關心內裡，報事/打斷後順手提醒帶傘/喝水/回信瑣事；compassion 0.15→0.60 同時觸發「冷諷較高」+「同情較高」兩條 flavor = tsundere）；**Marvin 冷淡看待一切**（對結果無所謂、日常事抽離成宇宙虛無）。Marvin 全域 preset 不動，冷淡只在 dual pattern block 強調。
- **bug 修**：LLM 偶爾把 speaker 標籤 echo 進 text（"Marvin: ..."），`_parse_segments` 加 `_strip_speaker_prefix` 清掉（regex 涵蓋半/全形冒號 + 中英名 + 疊多層）。

**還沒接 dual 的點**：ProactiveTopicAgent（走 play_tts priority=2 直接路徑、drop threshold 3s 跟 speak() 的 8s 不同，遷移要小心）、wake reply（IntentBus 出來的 Marvin LLM 回應，proactive=False 故意不接）。

---

**Phase 2 待做**（design doc 已列）：好感度 SQLite、`/favor` slash command、`tune()` 實作、派系觸發、social proxy、LLM judge 嘴賤評分。

## project_plan_b_public_bot
*Plan B（公開可邀請 bot）計劃已寫、冷凍待命；啟動 gate + Gemini 每 guild 成本*

**決策（2026-06-06）：要走 B「self-host，他人 invite Marvin 加入」公開 bot 模式，但「有客戶再開始」——計劃寫好冷凍待命，gate 沒亮一行 code 不寫。** 完整計劃在 `docs/PLAN_B_public_bot.md`。

**為什麼 B 而非 A**：B 給病毒式分發（每個語音房間 = N 人公開 demo → 別人房主想加），這是單人伴侶 app 結構上拿不到的迴圈。代價＝經濟模型與 Open-LLM-VTuber 相反：伴侶 app 用戶自付運算，**B 所有房間成本集中算 host 頭上**。

**啟動 4 條件（同時亮才解凍）**：①≥3 個外部房主主動要加 ②成本覆蓋方案（誰買單，因 $20/guild/月×N 上不封頂、已撞過 spending cap）③本地台連 2 週無 crash loop ④你有時間吃 ops（B＝你變別人 SLA 提供者）。任一沒亮＝維持自己玩+朋友手動加。

**Gemini 成本（真實量算，2026-06-06）**：Gemini 2.5 Flash $0.30/$2.50。單活躍 guild **≈$20/月**（規劃抓 $30 上緣，因現量被免費池 429 壓著、付費後更多呼叫成功）。組成：marvin_chat ~200 主回應/忙日=$0.30(成本主力,大 prompt 每輪重送)+背景~100=$0.14+STT~$0.06+cleaner~$0.01≈$0.51/日。⚠️**修正紀錄**：初版把 STT 寫成 200 句(grep 數錯欄位)、cleaner 寫成 200/日——真實 STT **~1,600-2,150 句/忙日**(數 stt_*.log `(Debounced)` 行,現由 Swift 本地免費分攤)、cleaner **gated 只 ~8-22/日**(多數轉錄只被動聽餵 context 不送 LLM)。兩錯反向抵消,$20 headline 不變。⚠️資料純度:呼叫量真實(llm_routing.jsonl)、token 數是估的(log 記 tokens:0,用 marvin_prompts.py ~7K token 庫推估)。**STT 別「全走 Gemini」**:$ 便宜($0.06/日)但 Gemini STT 每句多 ~500ms round-trip 打熱路徑=延遲災難。**⚠️ Groq vs Swift 已實測 A/B(2026-06-06,`scripts/stt_swift_vs_groq_ab.py`,n=4 真實 WAV):Swift 單句完勝**——中位 Swift 343ms vs Groq 525ms(Groq 慢 1.5x,RTT 吃掉運算優勢);Swift 對音長幾乎免疫(268-410ms),Groq 長到 1024ms;Groq 還輸出簡體(zh-TW 要轉)+這組品質更差(2s 幻覺/10.5s 掉內容,但 n=4 弱+我沒降採樣對它略不公)。**修正關鍵假設:Phase 1 走 Groq 不是升級是「降級換可移植性」**——Swift 更快更準但 macOS 綁死+Semaphore(1) 序列化(06-06 breakdown「排隊等 worker p50=415ms」單 guild 就露)。**Groq 對 B 唯一價值=能上 Linux+雲端並行不序列化,不是延遲;若 Swift 能上雲 B 根本不需要 Groq**。wake-check 設法留本地/便宜(丟 Swift 後喚醒檢查若也走雲端 STT,隱藏量比 2000 句更大+熱路徑延遲,啟動前先量)。TTS 保留 edge-tts 免費。壓低槓桿(啟動後)：context caching 砍 chat input 50-70%+Flash-Lite 跑 cleaner+Batch API 背景 → $20 可降 ~$6-10。外推：50 guild≈$1K/月、200≈$4K、1000≈$20K。
**⚠️ context caching 只對 Plan B 付費 Gemini 有意義,別對現在的 bot 提(2026-06-06 查證)**：現 marvin_chat 99.9% 走 Cerebras(862)+Groq(553) 免費層,兩家都是 **automatic prompt caching 零 code 已自動開**;且 prompt 已排對(`marvin_prompts.py:541/561` base_instruction 靜態人格在最前=可快取前綴,env_context 時間戳/memory 等動態全在後)。免費層 $ 本來 ~0,快取無 $ 可省。**現 bot 唯一降免費池 429 的招都有 quality tradeoff(拉高 wake 少回應/砍人格 prompt/加 provider),結論=別做,接受免費層現狀。**

**分階段(每階驗證綠才進下一)**：**P1 主機基座有兩條路,早期選 A（2026-06-06 實測翻案）**——
**路 A Mac mini host（早期首選）**:一台 M4 mini 當 host,**完整保留 macOS stack（Swift/davey/edge-tts/ffmpeg）零 STT 重寫**。依據＝Swift 單句完勝 Groq + **Swift 能並行**（`scripts/stt_swift_concurrency.py`:M1 8GB N=8 batch 僅 1468ms 非 OS 序列化,Semaphore(1) 是軟體選擇可調）。Swift 唯一死穴只剩 macOS 綁死→那就別逃 macOS。M4(不 swap+NE 3x)保守 2-3x M1 容量,錯開 duty cycle 服務低數十房間,早期 B 夠用。代價＝固定月成本高($600 一次性 mini 或 ~$100-200/月雲端 Mac colo)、擴張要加 Mac。**無 STT API 成本**。
**路 B Linux+雲端 STT（後期/大規模才需要）**:Dockerfile 已排除 macOS 套件半鋪好,STT 走 Groq（降級換可移植性）,驗 davey/DAVE Linux 能解密 `cryptoerror_storm_sentinel_blindspot`。**認真考慮路 B 才測 faster-whisper/FunASR GPU**(本機跑不動,租雲端 GPU 按小時 RunPod/Modal/Replicate,harness 仿 stt_swift_vs_groq_ab.py)。
→ P2 去單例(DiscordVoiceEngine 單例 main_discord.py:215 + voice_clients[0] 多處 → per-guild 路由、stt_lock 改 per-guild ~3 lane) → P3 多租戶治理(admission control K 房上限+per-guild 配額/consent/ZDR `project_relaxed_zdr_tiered_retention`+de-pin home guild env) → P4 公開邀請流 → P5 分片(真有量才做)。
**STT 並發放開（stt_lock Semaphore(1)→N）— 已驗證但 2026-06-06 決定「先不上、留 Mac mini」(選 A)**：實測 N=2=421ms≈暖機單發、**並發 ×4 轉錄與單獨一字不差(準確度安全已證)**、每程序只 +24MB(模型在共享 speechrecognitiond daemon)。**但不在現 8GB 機器上動**,因①它是有測試保護(`tests/test_concurrent_load.py::test_10_voice_stt_serialized` 斷言 max 並發==1)+CLAUDE.md L113 記載的刻意設計、註解「準確度優先」(此理由已被我數據破除但設計仍有測試)②8GB 閒置就 swap 邊緣(0.1GB free/2.9GB compressed,bot 還沒跑),加並發是打在「多人同講」痛點上的風險。**搬 Mac mini(RAM 有 headroom)後再放開**:改 stt_lock 值 + 更新 test_concurrent_load 斷言(==1→<=N) + CLAUDE.md L113;準確度已驗安全可放心調。

**已知地基**：資料層半多租戶(suki_memory for_guild、guild_id 穿進多模組)；阻斷點是語音 runtime 單例+stt_lock 全域+macOS 綁死 STT(Swift/mlx，現跑 M1 8GB 跑一條本地 Whisper 就 swap)。可搬零件評估見 `reference_open_llm_vtuber_parts`。

## project_relaxed_zdr_tiered_retention
*Marvin 隱私資料保留的方向決策——選分層保留而非硬 ZDR/fork；含 golden 蒸餾資料現況*

2026-06-01 評估「Marvin ZDR 版本」需求後，**選定寬放版 = 分層保留（tiered retention）**，否決硬 ZDR 與 fork。

**Why：**
- Fork 一個 ZDR 版維護成本最高（與 main 持續分岔，每個新 agent/pipeline 雙邊維護）。
- 硬 ZDR（原文用完即焚）會閹割掉 J1 改善迴圈、speech DNA、profile 重建等一半長期能力，且與規格自己的「Module B 追幾個月前決策」自相矛盾。
- 純技術 delta 其實小：raw text 落地點只有 7 處，全在 STT callback 下游同一條鏈。

**分層 TTL 模型（已實作部分）：** T0 音訊 WAV 秒級即刪（既有，寫在**專案根目錄** `tmp_stt_<uid>_<ns>.wav` 非 /tmp，`finally` os.remove）/ marvin.db transcripts SQL 原文 **14d prune**（prune_transcripts.py，live bot 最長回看 7d 故安全）/ 🟡 失敗訊號 judge·gaps·rescue raw **14d 轉 hash**（scrub_improvement_raw.py，clustering 要跨天累積故用 hash 非刪）/ 向量庫語意 embedding + profile/摘要 **長期保留**（記憶功能本體）。stt_history.log 是 rotating（既有）。**兩條 14d 規則由 3am `feedbackbatch`（run_feedback_batch.py → zdr_scrub + transcript_prune）執行**，6/5 驗證實跑（deleted 682 rows、scrub 0 因資料未滿 14d）。

**2026-06-05 審查 + README 重寫（四層 Seconds/Hours/Days/Long-term + 驗證指令）：** ⚠️ 審查發現「**Hours 層其實是 size-capped 不是 time-capped**」（bot_stdout/stt_history/主 log 都 RotatingFileHandler 按 maxBytes 輪轉，低活躍時可存數天）——在隱私政策叫「Hours」會 over-claim，README 已誠實標註「size-bound, not a hard time guarantee」+ 註腳。若日後要真 Hours 保證＝改 `TimedRotatingFileHandler`（user 6/5 暫選誠實版 A 不改 code）。README「Data retention」段現含五條 read-only 自驗指令（WAV 數 / scrub 跑過沒 / hash 計數 / rotation 檔 / launchctl）。

**suki_golden_dataset 現況（Operation Distillation）：**
- 由 gemini_router_content 兩呼叫點累積（社交分析 JSON + 補位台詞自由文本），目的：蒸餾出本地模型取代雲端 Gemini → **本身就是 ZDR 盟友**（停送雲端）。
- **無任何 trainer/consumer**，collect-only。audit 顯示僅 ~30% 可蒸餾（schema 飄移 15+ 變體、string-bool 366、pipe-enum 77、完全重複 256）。
- v1 消費口定為 **audit 報告**（不是 replay-eval）——replay 要等資料正規化後才有意義。
- **正規化已完成**（normalize_golden_dataset.py）：投影到最小共同 **3-key** schema `{social_gap, confidence, sentiment}`，1491 → **632 筆**乾淨樣本，輸出 records/suki_golden_normalized.jsonl（gitignored）。social_gap 收斂成 4 類（縮寫 info/redir/emo → 全名）；砍掉 intervention（65% null）。**下一步才是真正 fine-tune / 蒸餾**（尚未做）。

**How to apply：** 談 ZDR/隱私/資料保留時走分層，不要再提 fork 或硬刪。要蒸餾就用 suki_golden_normalized.jsonl（已是 fine-tune Messages 格式）；scrub 用 hash 指紋（非清空）以保住 analyze_agent_gaps 的 distinct 計數。三隻腳本：audit_golden_dataset / normalize_golden_dataset / scrub_improvement_raw。

## project_spontaneous_manzai
*自發漫才（不依賴 openclaw）+ 打岔疊播 mixer 雙層；觸發條件與下一步*

2026-06-03 做的 Marvin+Marmo 漫才鏈（commit efed1a0 自發、3e1d792 打岔）：

**背景**：原漫才只在外部 openclaw POST `/marmo-result` webhook 時觸發（MarmoServer 是 Marvin 進程**內**的 webhook，不是獨立進程；`ps grep marmo` 查不到≠沒跑）。openclaw 從沒主動推 → 漫才一次沒演。`MARMO_DUAL_CHANCE` 6/2 從 1.0 降 0.3 因為高頻搶爆 LLM bus 害喚醒 429。

**自發漫才**（`intent_agents/spontaneous_manzai_agent.py`，SpeakBus agent）：不等 openclaw，冷場時 Marvin 自生雙人吐槽。env `SPONTANEOUS_MANZAI=true`（run_bot.py 已開）。觸發全 AND：靜默≥120s + 距上次≥30min + 有觀眾 + 有 recent_utterances 取材 + conf 0.4 沒被 ProactiveTopic(0.6) 蓋過。複用 `generate_dual_dialogue(pattern="marvin_lead")` + `play_dual_dialogue`。

**打岔**（mixer 雙層，6/3 實機調好）：`local_mixing_source.py` 加 layer2（push_tts2/_next_tts2_frame，與 layer1 並行 mix_layers 相加）。`_stream_tts_to_mixer(layer=2)` + `_play_dual_interject`（Marvin→layer1 串流，到算出的時機後 Marmo→layer2 疊進）。`play_dual_dialogue(interject=True)`；非 Plan12 落序列。

**實機調出的參數**（用戶聽感拍板）：
- `_interject_duck=0.6`（Marvin 淡到的底音量；0.45 太低被蓋成「完全退位」）
- `_interject_step=0.008`（fade ~1s 逐幀 ramp；瞬降太突兀）
- 切入時機 **動態算** `manzai_interject.compute_interject_ratio`（base 0.72 微調到落子句中段、避標點 → 不同對白都切句中像真打斷，非固定值卡標點）
- 漫才走 **protected**（演出唸完不被 barge-in 中斷，否則串流被 kill→餵入中斷沒聲音）

**測試後門**（免 LLM、免重啟）：webhook payload 帶 `segments`(現成對白跳過生成) + `interject`/`duck`/`step`/`at`(即時 taste-tune)。curl 範例 + token 在 .env::MARMO_TOKEN。`scripts/test_manzai_gen.py` 測純生成。

**✅ 6/3 15:44 live 驗證通過**（用戶拍板 OK）：webhook 開一槍 `armed=True marvin=582幀 marmo=184幀`，走 `_play_dual_interject` 非 fallback，read_ms max 6.16ms 無 underrun。機制+聽感都綠。下一步：deferred 的 **TTSScheduler refactor**（plan 在 `~/.gstack/projects/butthead0819-beep-marvin-voice-core/jackhuang-main-design-Plan12-Scheduler-20260603-085249.md`，已決「先跑穩現版幾天再開工」）。

**踩過的雷**：每次重啟踢出語音頻道要重 summon；Plan12 mixer 是 on_demand，_play_dual_interject 串流期間要持續 re-arm adapter；text 太短會被 guard agent(0.96) 搶走 dual_speak(0.95)；Plan12_Stats 是 print 落專案根 log 不在 ~/Library。相關 `project_plan12_local_mixing` `project_llm_pool_attribution`。

## runtime_state_files
*Bot 啟動時讀的本地 state files 清單，遷移／clone 時要從舊環境複製過來，否則 bot 看起來會像「死」但其實只是 init 後狀態歸零*

bot 啟動時會 init 一堆本地 JSON / JSONL 檔，這些**沒進 git**（gitignored）但 bot **必讀**。遷移到新資料夾或重 clone 時要從舊環境帶過來，不然會出現各種「STT 沒反應」「人格重置」「歌曲記憶歸零」等症狀。

**必補檔（漏一個 = 對應子系統失能）**:

| 檔 | 內容 | 漏了 bot 行為 |
|---|---|---|
| `consent.json` | `{"consented": {speaker: bool}}` | **STT 完全不動** — `handle_stt_result` 第一行 `if not consent.is_consented(speaker): return` 直接退出，wake / cleaner / intent 全跑不到 |
| `wake_stats.json` | wake fusion per-speaker confidence stats | wake fusion 從 0 重新累積，前幾分鐘 false-positive / false-negative 變多 |
| `departure_stats.json` | 離場行為統計 (per-user 預測模型) | 離場預測冷啟 |
| `suki_dna.json` | Marvin 人格 DNA 演化值 (toxicity / persona_tag …) | 個性重置（從預設值開始演化） |
| `suki_budget.json` | LLM budget 累計花費 | budget 計數歸零（cost alarm 失準） |
| `suki_memory.json` | per-player 偏好 / personal_info / interaction_count | 玩家記憶歸零（馬文「認不出」常客） |
| `music_memory.json` | 歌曲 metadata + URL 緩衝 / stt_corrections | 點歌要重新 yt-dlp 解析，慢 |
| `game_log.jsonl` | 遊戲歷史 append-only log | 純 log，bot 行為不受影響但歷史分析會少一段 |

**Why**:
- 2026-05-24 從 `~/Documents/Antigravity/Discord-voice-bot/` 遷移到 `~/Code/Discord-voice-bot/` 時，**只 clone git 內容，gitignored state 沒帶過來**。最痛的是 `consent.json` 缺檔 → 所有 STT 結果直接被 `handle_stt_result` 第一行擋掉 → 看起來像 STT 沒反應，實際上 raw STT 都正常，只是 callback 全 return 0。
- 花了一個多小時排查，debug 路徑：DAVE 解密 ✓ → Swift STT 輸出 ✓ → `[STT Output]` 出現 ✓ → 但 `stt_history.log` 沒任何 wake 紀錄 ✗ → 最後才想到 `handle_stt_result` 的 consent gate

**How to apply**:
- **第一次 clone / 新環境 setup**：從舊環境 / 備份還原這 8 個檔，否則一堆 default-empty state 會讓 bot 看起來像有 bug
- **「STT 沒反應」debug 流程**：raw `[STT Output]` 有出現但 `stt_history.log` 沒任何 `[⚡喚醒]` / `[Debounced]` / `[✅Query通過]` → **第一個檢查的就是 `consent.json`**，這是最常被忽略的 silent killer
- **不要硬覆蓋**：如果新環境 bot 已經跑過、有累積新 state，merge 比覆蓋安全（特別是 `music_memory.json` / `suki_memory.json`）。先 `launchctl bootout` 停 bot 避免反向覆蓋
- **`music_memory.json` 結構**：top-level `{"songs": {url: {...}}, "recommendations": {}, "stt_corrections": {}, "recent_recommendations": []}`，merge 時 songs 用 dict update（key 衝突取新），其他用相應結構合併

**Anti-pattern**:
- 看到 STT pipeline 全段沒反應就懷疑 STT engine、DAVE、voice_recv、wake_detector — 結果都不是，是 consent gate
- 直接覆蓋而不備份 — 萬一舊版有問題或結構不相容會丟掉今天累積的 state

## speakbus_and_survival
*Marvin 主動發話用 bid 架構（SpeakBus），以及 agent 自調/求生能力的分級路線圖與陷阱*

## 決定（2026-05-24 討論）

Marvin **主動發話**（社交補位 / 5 分鐘日誌 / Memory callback / 龍蝦回應等）將走 **SpeakBus** — IntentBus 的 outbound 鏡像版。未來新主動行為一律用 bid 架構設計。

**Why**：目前各主動行為獨立觸發 → TTS 互卡 / 同時搶話 / 互相打斷。bid 解決「只一個贏」+ 統一 mode gate + 統一 outcome log。

**How to apply**：使用者下次叫我寫主動發話相關功能（不論是新行為或調整既有的），預設用 SpeakAgent 模板，不要再寫獨立 trigger loop。喚醒回應**留在 IntentBus**（被動回應，性質不同）。

## SpeakBus 跟 IntentBus 的關鍵差異

| | IntentBus | SpeakBus |
|---|---|---|
| 觸發 | 單事件（一句話） | 連續時間 + 多事件 tick |
| 預設 | 一定有 winner | 預設**沒人講話**（silence is the default） |
| bid 語意 | 我最懂這句話 | 我現在發話的合理性 |
| MIN_CONFIDENCE | 0.30 | 要拉很高（0.5+） |
| 額外需要 | — | per-agent cooldown、tick context logger |

## Quality signal 問題（很重要）

SpeakBus **沒有 ground truth**。沉默 ≠ 該沉默。所以：
- 離線 replay 只能驗 **mechanical**（頻率/衝突/餓死）
- Shadow mode 可以收 context，但 quality 仍需人工抽樣標
- 任何「自調」機制都受這個訊號稀缺性的限制

弱代理訊號可用：**「贏 bid 之後 N 秒內有 STT」= 算正向**。不是真 quality，但比沒有好。

## 求生能力 — 分級路線圖

**直覺寫法會壞掉**：如果單純「輸=痛、贏=爽」，所有 agent 學會 bid 0.95 on everything → bus 退化成 noise。求生不能是「叫大聲」。

由保守到激進的 4 種詮釋（推薦依序進化）：

1. **餓死警報**（不自調，只通報）— 連 N 天沒贏 → log warning，人來判斷
2. **Cooldown 自調**（bounded）— 從沒撞 cooldown 就拉長、一直撞就縮短，**有上下限**
3. **利基特化**（推薦核心方向）— agent 記錄 sweet spot context（時段/mode/溫度/前一句主題），sweet spot 內 bid 升、外 bid 降。**抗脆弱**：輸越多學越多哪裡不要 bid。求生 = 找到「我唯一能贏的場合」，不是「叫大聲」
4. **內部能力演化**（最激進）— LLM-based reflection 週期 review outcome log，改 prompt / trigger keyword。風險：reproducibility 變差、debug 難、需 rollback 機制

## 實作順序（建議）

1. SpeakBus 骨架 + 餓死警報（第 1 種）
2. 加 outcome log（贏/輸 + 後續有沒有接話）— 為第 3 種鋪資料
3. 利基特化（第 3 種）作為第一個自調機制
4. 內部能力演化（第 4 種）等第 3 種證明後，挑**一個** agent 試做（建議 MemoryCallback）

**強制要求**：每個自調機制都要有 **kill switch + outcome log**，可隨時關掉看純規則版對比。

## speculative_stt_pipeline
*bus 入口前用 J1 Regex / J2 Groq-8B / J3 Cleaner 三路 judges race，最快達信心門檻者勝出*

**架構方向（規劃中）**：把 `STT → cleaner → bus` 的序列改成 bus 入口前的 **parallel judges race**。借 LiveKit/Vapi 的設計模式，**不**借框架（audio 層綁 Discord DAVE/py-cord，換不值得）。

當前序列：
```
Swift STT final → cleaner LLM (~400~600ms) → intent_bus.bid() → handler
```

目標：`isFinal=true` 瞬間，三路 judges 並行 race，winner 直接 dispatch：

| Judge | 延遲 | 信心門檻 | 角色 |
|---|---|---|---|
| **J1 RegexJudge** | <5ms | 命中即 0.95 | 讀 `DeclarativeIntentAgent.declare_intents()` schema，結構化指令 |
| **J2 SmallLLMJudge** | ~150ms | ≥0.8 | Groq 8B classifier，處理同義改寫/口語化 |
| **J3 ClenerJudge** | ~400~600ms | bus winner conf | 現有 `stt_cleaner.py` 路徑，碎片/語助詞 fallback |

Race 規則：
- J1 命中 → 立刻 dispatch，cancel J2/J3
- J1 miss / 低信心 → 等 J2；J2 ≥0.8 → dispatch，cancel J3
- J2 也不確定 → J3 跑完（never worse than status quo）

**Why:** Marvin 的 `intent_bus.py` 已經是 bid market（agents 並行 bid、`MIN_CONFIDENCE=0.30` 過濾），真正硬編碼的是 bus 之前——`STT → cleaner → bus` 序列單一路徑。把 cleaner 從「必經之路」降級成「judges 之一」，是改動範圍最小、延遲收益最大的切點。

**How to apply:**
- bus 跟 agents 完全不動
- 新檔：`intent_judges/regex_judge.py`、`intent_judges/small_llm_judge.py`、`intent_judges/cleaner_judge.py`（包裝現有 stt_cleaner）
- voice_controller 入口加 race coordinator（asyncio.wait + cancel 邏輯）
- 動 J1：純函數，零副作用，先 TDD 完整 unit test 再接

**進度（2026-05-24 shadow mode 上線）**：
- ✅ `intent_judges/regex_judge.py` (commit `082e08a`)
- ✅ `intent_judges/race.py` + RaceResult/JudgeOutcome instrumentation (commit `082e08a` → `33070c3`)
- ✅ `intent_judges/small_llm_judge.py` — J2 rewriter 純函數，DI 注入，**未接 prod LLM** (commit `082e08a`)
- ✅ `intent_judges/cleaner_judge.py` — J3 cleaner adapter 純函數，DI 注入，**未接真 stt_cleaner** (commit `98fda0e`)
- ✅ `intent_judges/telemetry.py` — write_race_outcome → jsonl (commit `33070c3`)
- ✅ `intent_judges/voice_integration.py` — make_shadow_specs / run_shadow_race (commit `3b0b0ce`)
- ✅ voice_controller 接 shadow race（**只 J1 + J3 precomputed，零額外 LLM**）(commit `7b866ef`)

**Shadow 行為**：每次 `_process_queued_query` 走到 `_intent_bus.dispatch` 都會
fire-and-forget 跑 race，寫 `records/judge_outcomes.jsonl`，不影響主路徑。

**77 tests / 1.29s green，零 regression。**

---

## J2 SmallLLMJudge 待辦（prod LLM 接線）

shadow 目前**不含 J2**（避免每 utterance 多打一次 LLM）。要不要加 J2 等
2026-05-27 看 J1+J3 數據決定。決定加之後要做：

1. **選 model**：Groq Llama 3.1 8B（沿用 `stt_cleaner._stt_router` 的 `llm_pool`
   pattern）vs Cerebras vs Gemini Flash。考量：p50 latency、quota、context cost
2. **Prompt design**：input = raw STT + 從 declarative agents 抽出的 intent menu
   （e.g., "music_play / music_skip / nemoclaw / ..."）；output = JSON
   `{"rewritten": str, "confidence": float}`
3. **Adapter 檔**：寫 `intent_judges/llm_rewriter_adapter.py`（或併入 voice_integration），
   把選定 client 包成 `LLMCall` signature `Callable[[IntentContext], Awaitable[tuple[str, float]]]`
4. **Quota/fallback**：rate-limit / quota 用盡 → 安靜 dense-zero（small_llm_judge.py
   已有 catch-all，adapter 內也要先用 quota service 預判）
5. **加 J2 spec 到 shadow**：`make_shadow_specs` 多回一個 spec，並加 `_J2_THRESHOLD = 0.80`
6. **校準 threshold**：0.8 是猜的，prod 跑一段時間後依 J1 vs J2 winner agreement 調
7. **Tests**：mock LLM client，驗 JSON schema 解析、malformed → dense zero、
   timeout/quota → dense zero

---

## J3 ClenerJudge 待辦（真 stt_cleaner 接線）

shadow 目前 J3 用 **precomputed cleaner**（closure 直接回 caller 已 clean 的字串），
所以 J3 在 race 內 **不會真的呼叫 LLM**——這版只能驗證「若 cleaned text 拿來跑 regex
會不會中」，**不能驗證 J3 包裝真 cleaner 的端到端正確性**。

數據確認要走 authoritative 後才接真 cleaner。要做：

1. **真 cleaner adapter**：把 `stt_cleaner.AppRouter.clean_stt_text(raw)` 包成
   `CleanerCall` signature `Callable[[IntentContext], Awaitable[str]]`，**只取
   回傳 dict 的 `"text"` 欄位**（dict 還含 `is_wake / wake_intent / wake_threshold`，
   不歸 J3 用）
2. **Wake side-effect 隔離**：`clean_stt_text` 內部會跑 wake_fusion + 寫 stt_corrections.jsonl
   + 更新 local corrections。**race J3 路徑不該重複觸發這些**——adapter 要走「純清洗」
   旗標 or 開一個 internal-only entry point；務必避免 double-count wake intent
3. **Authoritative 切換條件（先驗）**：分析後 J1 hit rate ≥30% 且 J1/J3 一致率 ≥90%
   → 才考慮把 voice_controller 的 `_process_queued_query` 中 cleaner+bus 序列換成
   race 路徑（cleaner 從必經之路降級成 J3 一條 judge）
4. **Voice_controller patch**：authoritative 上線時，現有 cleaner 呼叫
   （cogs/voice_controller.py:3373 那條）要移除或變成「race miss 才退回」的 fallback；
   shadow 的 `asyncio.create_task(run_shadow_race(...))` 換成 `await race(...)` 直接
   dispatch
5. **Tests for real adapter**：mock `clean_stt_text`，驗證 dict→str 抽 `"text"`、
   cleaner 例外被 cleaner_judge 內的 except 接住、wake side-effect 不會在 race
   路徑被誤觸發兩次（hardest 的部分）

**改善迴圈**：見 `j1_improvement_loop.md`（confidence calibration / schema mining / keyword 擴充三路）

**下次回來分析**：見 `judge_outcomes_analysis_followup.md`（2026-05-27）

**Partial 用途（疊加層，可選後做）**：
- Apple Speech `shouldReportPartialResults=true` 不傷 final 品質
- **守則：partial 只能驅動可逆操作**（預熱 handler 資源、預掃 J1 候選），絕不在 partial 階段執行 handler / 吐 TTS / 換歌
- Cleaner 救不回 partial 的錯認詞（cleaner 看不到音訊，是清發音歪不是改 ASR 結果）

**未解設計問題**：
1. J2 小模型 schema 怎麼設計（intent enum + slots JSON）
2. 三路 race 的 cancellation 語意（J1 已開始呼叫外部 API 的 handler，被 J2 超車怎麼辦——目前共識：J1 命中即 commit，不被超車）
3. J3 cleaner 路徑保留 streaming 還是維持 one-shot

## stt_corrections_cache_and_pipeline_completeness
*想優化 cleaner/wake 效率或做「per-user 口音學習/三專家投票」前先讀——資料說邊際太小，真價值在修 corrections 快取兩個 bug*

2026-06-04：探索三個「降低 cleaner 依賴」的點子（三專家投票 → per-user 口音學習 →
喚醒詞 mishear 擴充），資料一致打臉「效率」框架，但挖根撈出兩個真 bug。已修+land（PR #22，commit 62fe52f）。

## 結論：cleaner/wake pipeline 已經很完整，效率邊際小
- **cleaner 一天只被呼叫 8-22 次**（latency_breakdown 06-01~04），gate 已丟 12507 句。
  再聰明的 gate/投票也只省一天幾筆。
- **穩定可學的 STT 修正 pattern 僅涵蓋 ~5-7%**（9 條全域短替換涵蓋 3% + 英文 filler 4%）；
  其餘 93% 是 context-dependent 長句，同 raw 對到不同 clean，**沒有穩定映射可學**。
- **per-user 無訊號**：showay 413 筆修正、重複 ≥2 的短 pattern 掛零；穩定的（Okay.→喔、
  Siri→馬文）三人都一樣＝**全域 ASR 怪癖，不是個人口音**。
- **喚醒詞清單已完整**（WAKE_WORDS_LIST 13 變體）：真正重複的漏接不是已被音樂 gate 接住
  （播放*→走 IBA-T0 wakeless 救援），就是 Siri（drops 45 次真在用 Siri，加了狂誤喚醒）。
  → B（擴充喚醒詞）**不做**：負期望值。

## 真價值＝挖根時撈出的兩個 corrections 快取 bug（已修）
`scripts/analyze_daily_log.py::build_stt_corrections_dict` + `_flatten_corrections`：
1. **遞迴巢狀腐爛**：寫檔把整包舊 dict（含 _updated/corrections）包進新 corrections key，
   每跑一次多巢一層 → `records/stt_corrections.json` ~25 層、無限長大，reader 只看頂層。
2. **歧義條點錯歌**：exact-match 快取（stt_cleaner.py:230）收了歧義條——raw「馬文播放音樂」
   歷史對到「播放音樂/播放周杰倫/第一次 70b」三種 clean。使用者完整說該句時可能被改寫成
   點錯歌。修：同 raw 須單一主導 clean(≥70%)才收，否則整條丟。
prod 檔已重建 25層→34條乾淨 flat。測試 `tests/test_stt_corrections_dict.py` 4 case。

## 三專家投票設計（NO-GO 但留檔）
`docs/triadic_expert_stt_gate_design.md`：若未來真要做，正解＝**壓力下條件式入口節流**
（quick pool 全冷卻才啟動投票），不是全面 gate，也不是 tier 升級點（升 analyze 是
quick 整池冷卻造成，投票變不出算力）。go/no-go 前置＝先量 under_pressure 佔比。

關聯 `cleaner_latency_and_response_failrate` `iba_t0_wakeless_music` `feedback_data_driven_diagnosis`。

## stt_diagnostic_signals
*抱怨「STT 沒反應」時要按什麼順序看哪幾個 log、區分 5 種失敗模式*

「STT 沒反應」是 user 最常見的回報，但底下至少有 5 種獨立失敗模式。**先看信號才下藥**，不要直接重啟。

**信號表**（在 `bot_main.log` / `bot_stdout.log` / `stt_history.log` 搜這些 pattern）:

| 信號 | 失敗模式 | 修復方向 |
|---|---|---|
| `nacl.exceptions.CryptoError` 大量出現 + 0 個 audio packet | **DAVE 內層沒解** — voice_recv 在新 guild 啟用 DAVE 後完全沒接 davey | 看 `voice_pipeline_dave_to_stt.md` |
| `Received packet for unknown ssrc XXXX, size=12` 連續多筆 + 沒任何 audio-size packet (≥50 bytes) | DAVE handshake 失敗或 voice gateway 升級 | 看 op 4 中 `dave_protocol_version` 是否升到 v2+；查 davey 版本 |
| `[STT Output]` **有**但 `stt_history.log` 沒任何 `[⚡喚醒]` / `[Debounced]` / `[✅Query通過]` | **`consent.json` 缺檔或 user 沒同意** → `handle_stt_result` 第一行 `if not consent.is_consented(speaker): return` 全部退出 | 確認 `consent.json` 存在且該 speaker 在 `consented` 為 true |
| `[STT Drop] wake_inflight=2 ≥ 2` 持續刷 | wake_check 並發 slot 卡死，Swift STT subprocess 沒回應 | `discord_voice_engine.py` 內 `_wake_inflight` 釋放邏輯（line 940-949 idempotent closure），重啟 bot 暫解 |
| `[Swift STT] Exception: [Errno 2] No such file or directory: './macos_stt_bin'` | bot cwd 不對 / launchd `WorkingDirectory` 跑錯 | 看 `bot_run_topology.md` |
| `ps aux \| grep main_discord.py` 看到 2 個程序 | 兩隻 bot 互踢 Discord gateway session，session_id 一直被搶 | 殺 orphan (PPID=1)，留 launchd 託管的 |
| `stt_history.log` 一堆 `[Debounced] __META__ {...}` 沒文字內容 + 大量 `[STT Fatal] 所有辨識方案皆失敗` | **engine 跑 51771d8 (2026-05-24) 之前的版本，沒裝 META filter** — Swift 辨識文字為空時 `__META__ {...}` 行被回傳當 text 一路洩漏到下游 | 確認 `discord_voice_engine.py` mtime > bot 啟動時間？是 → `launchctl kickstart` 重啟讓新 code 生效；若沒新 code 就要先 `git pull` |

**Ground truth 「STT 真的有跑」**:
- `bot_stdout.log` 應該有 `✅ [STT Output] <speaker>: <text>` 連續出現
- `bot_stdout.log` 應該有 `🚀 [Sink] 捕捉第一筆有效語音 (DAVE+) 來源: <name>`
- **不要看 `stt_history.log` 判斷**：那只記 BOT降臨 / 嘲諷 / 喚醒成功 / 點歌等高層事件，原始 STT 不寫進去。STT 在跑但沒喊「馬文」喚醒詞 → stt_history 看起來像沉默，其實 Swift STT 都有跑完

**Why**:
- 5/22 DAVE rollout 之後我們花了 1 個多小時才釐清是 DAVE 不是路徑遷移，因為兩個現象同時發生（剛好遷移到 `Code/`、剛好 STT 死）→ user 很合理地懷疑是遷移；最後靠舊資料夾 stt_history.log 在 5/22 20:30 之後也沒成功 transcription 才證明是 Discord 端問題
- wake_inflight 卡死跟 DAVE 是**獨立**的兩件事，不要混為一談；DAVE 解開了但 wake_inflight 卡也會看起來 STT 沒反應
- 2026-05-24 META 洩漏事件：bot 從 19:23 跑，working tree 在 22:24 加了 META filter 但沒重啟，使用者 22:44 回報「沒看到內容」→ 對 `ps -o lstart=` 跟 `discord_voice_engine.py` mtime 一下就抓到。**stale process running stale code 是個獨立失敗類別**，diagnostic 流程要列入「對時間戳」這步

**How to apply**:
- 收到「STT 沒反應」先 `tail -100 bot_main.log` 看 CryptoError 數量，再 `tail -100 bot_stdout.log` 找 `STT Output` / `Sink 捕捉` / `STT Drop`
- 多進程現象一定先檢查：`ps aux | grep main_discord.py | grep -v grep | wc -l`
- **stale process 對時**：`ps -o lstart= -p <PID>` vs `stat -f "%Sm" discord_voice_engine.py macos_stt.swift marvin_voice_core/stt_handler.py`，bot 啟動 < 檔案 mtime → 需要重啟才能套用
- 不要在沒看信號之前就重啟 bot — 重啟會清掉 in-memory 狀態，讓現場消失；但 log 不會丟

## triadic_expert_pattern_domain_and_timing
*三專家(positive/negative/biased)投票模式何時 work——用在離散穩定 token 的域 + 把 biased expert 拆去離線 curate；wake 系統是活證明*

2026-06-04：使用者提的「三角專家投票」(E1 positive / E2 negative / E3 learned bias)
我一度為 cleaner gate 判 NO-GO。後來使用者點破——**這結構其實一直活在 wake 系統，而且在那裡 work**：

| 三專家 | wake 系統 | 角色 |
|---|---|---|
| E1 positive（我/播/聽/想/要） | `WAKE_WORDS_LIST` | 「這是意圖」正向 pattern |
| E2 negative（你/不/別/停） | `removals guard`（wake_words_override.json） | 「這不是」否決 |
| E3 biased（歷史 true/false bias） | `addition guard`（filter_unsafe_wake_additions，drops 頻率） | 從負空間學、curate 名冊 |

## 為什麼 wake 域 work、cleaner-gate 域不 work（兩個條件）

**1. 域要「離散穩定 token + 正負類乾淨可分」。**
wake 詞短、重複，合法近音詞在 cleaner_gate_drops 出現 **0 次**、日常詞 7 次 → 鴻溝清楚。
cleaner gate 面對糊掉整句（48% 兩專家都不中、同 raw 對到不同 clean）→ 正負類糊在一起，
投票投不出信號。**糊 raw 上的 keyword 投票 == 雞生蛋（最需要清洗的句正好抓不到）。**

**2. 時機要「把 biased/learning expert 拆去離線」。**
原始提案想三專家**同時 per-utterance 對糊 raw 投票** → 死。
wake 系統拆開：
- E3(biased) 在**離線/每日/aggregate 資料**跑，curate 名冊（昂貴+有雜訊的學習丟離線）
- E1/E2(positive/negative) 在 **runtime/per-utterance** 跑，只做乾淨離散比對
→ runtime 快、學習慢，各得其所。

## 暗合的既有紀律
- E3 是**只拒不推**的非對稱學習器 → 同 `j1_improvement_loop`「confidence 只下調」、
  IntentBus negative-space Bid。
- 學**負空間**（drops = 什麼不是喚醒）→ 同 `feedback_trigger_excludes_sentinels`。

## 可複用原則（下次想做投票/評分 gate 先過這關）
> 三專家(positive/negative/biased)模式對的；但只在 **(a) 離散穩定 token、正負類可分的域**，
> 且 **(b) biased expert 拆去離線 curate、positive/negative 留 runtime** 時才成立。
> 糊 raw 的 per-utterance 三方投票會死。

關聯 `stt_corrections_cache_and_pipeline_completeness`（設計 doc 已改寫成此 reframe，非 NO-GO）。

## voice_pipeline_dave_to_stt
*STT 核心服務的解密依賴鏈，Discord 啟用 DAVE 後 voice_recv 解外層 SRTP、davey 解內層 E2EE，斷一層 STT 全死*

語音 audio 從 Discord 到 STT 必須通過**雙層解密**：

```
Discord UDP packet
  → voice_recv.AudioReader.callback (reader.py:136)
      → decryptor.decrypt_rtp (我們 patch 過的)
          ├ orig_rtp(packet)                    [外層: SRTP aead_xchacha20_poly1305_rtpsize]
          └ _maybe_dave_decrypt(packet, plain)  [內層: davey E2EE, 用 dave_session.decrypt(uid, MediaType.audio, ct)]
  → RealtimeVADSink.write (discord_voice_engine.py:243)
      → VAD 切片 → Swift STT subprocess
```

**Why**:
- 2026-05-22 20:30 Discord 對該 guild 啟用 DAVE protocol (op 4 訊息含 `dave_protocol_version: 1` / `secure_frames_version: 1`)
- voice_recv 0.5.2a179 完全不知道 DAVE 存在，只解外層 SRTP；內層 davey ciphertext 直接灌進 Swift STT 變亂碼 → STT 全 0 結果
- 自此 stt_history.log 從每天數百筆 transcription 變 0 筆，bot 看起來「活著但聾」
- discord.py 2.7.x（PR #10300）已內建所有 DAVE opcodes (21-31) 跟 MLS group state，會自動維持 `voice_client._connection.dave_session`，**我們只需要接最後一步 decrypt**

**How to apply**:
- **STT 沒反應第一個查的就是 DAVE 鏈**：看 `bot_main.log` 是否還有 `nacl.exceptions.CryptoError` / `[KeySync] RTP key 同步失敗`，看 `bot_stdout.log` 是否出現 `[Sink] 捕捉第一筆有效語音 (DAVE+)`
- 整合點是 `discord_voice_engine.py::patch_voice_recv_key_sync()`（被 `cogs/voice_controller.py` 5 個 summon 路徑呼叫）— 不要把 DAVE 邏輯散到別處，所有解密合在這一個函式內
- 守備條件：`voice_client._connection.dave_ready` 為 True 才呼叫；`_ssrc_to_id.get(packet.ssrc)` 拿 user_id；davey 拋例外回 SRTP plaintext (passthrough 模式本來就是明文)
- 不要假設「升 discord.py / voice_recv 就會自動修好」— voice_recv 沒有 DAVE PR，未來升級依然要保留這個 patch（或者改丟到 voice_recv subclass）
- 改完一定要 live 在語音頻道測試（mock 不會跑到真 SRTP/MLS handshake）
- 看「成功」的 ground truth：`bot_stdout.log` 有 `✅ [STT Output] <speaker>: <text>` 連續出現，**不是** stt_history.log（後者只記 BOT降臨/嘲諷/喚醒成功，原始 STT 不寫進去）

**關鍵依賴版本**（升級前留意）:
- `davey == 0.1.5` (Snazzah/davey, Rust 寫的 MLS 實作)
- `discord.py == 2.7.1+` (要含 PR #10300, 內建 DAVE handshake)
- `discord-ext-voice-recv == 0.5.2a179` (沒有 DAVE 支援, 必須 patch)

**Anti-pattern 警告**:
- `davey_bridge.py::apply_davey_fix()` 那個 "DaveSession→MLSContext shim" 是給 Pycord 用的，這 repo 是 discord.py 完全用不到（但留著也沒副作用，主要是它的 macOS UDP 修補有用）
- 不要試著手動處理 opcodes 21-31，discord.py 已經接好了；只接 decrypt


---

# 🔖 Reference — 外部資源 / 評估結論

## reference_open_llm_vtuber_parts
*Open-LLM-VTuber 評估結論——對 Marvin 只剩 2 個可搬零件，其餘不值得*

評估過開源專案 **Open-LLM-VTuber**（github.com/Open-LLM-VTuber/Open-LLM-VTuber，語音 AI 伴侶，ASR→LLM→TTS→Live2D，主打離線+多後端 config 可插拔+Live2D 頭像，單機單人）。

**結論：對 Marvin 只有 2 個「補強型」可搬零件，現在都不缺、不用動 code，當貨架記著、遇到對應痛點再抄：**
1. **SileroVAD**（`src/open_llm_vtuber/vad/silero.py`）— 神經網路語意 VAD（估每 frame 人聲機率），補我們能量法（滾動 RMS 噪音地板）在串流播放時的擴音回聲誤觸發。當第二道閘，不換掉自適應噪音地板。見 `voice_pipeline_dave_to_stt`。
2. **FunASR / sherpa-onnx**（`src/open_llm_vtuber/asr/`）— 中文 STT 補強（FunASR 阿里中文強）或純本地最終 fallback，照我們 `STTService` Protocol 包、不動 race coordinator `speculative_stt_pipeline`。

**不值得搬：** TTS engine 庫（Marvin 主聲道已是 Edge TTS；`/marvin_say` 走 macOS say 只是給不能開麥者的玩具，刻意不碰）；agent/conversations（它是直球 ASR→LLM→TTS，無 IntentBus 競價/多人 speaker 歸因/game 模式，我們的 IntentBus 比它成熟）；server/websocket/live2d/routes（Web 前端世界，跟 Discord 無關）。

定位：它強在「離線+多後端+Live2D」廣度；Marvin 強在「多人 Discord+IntentBus 競價+STT 投機管線」深度。骨架同物種，賭注不同。

**為什麼它 ~10k star / 安裝爆（不可複製到 Marvin）：** 不是某大 Vtuber 在用，而是 README 宗旨明寫「開源復刻閉源的 Neuro-sama」——直接接住 Neuro-sama（Twitch 訂閱前三的現象級 AI VTuber）幾十萬粉「我也想要一個」的現成需求池。增長三引擎＝①蹭 Neuro-sama 狂熱社群 ②本機+免費+隱私的「私密 AI 女友」(用戶見證「被用 10 萬次") ③Live2D waifu 視覺情感投射 + 亞洲二次元伴侶市場(中/日/韓 README+QQ 群)。**這三個引擎 Marvin 全沒踩**：無對標爆紅 IP、多人公共語音(社交非私密陪伴)、純語音無頭像、做工具/DJ/遊戲主持。它賣情感陪伴幻想，我們做多人房間智慧夥伴＝不同物種不同慾望。教訓：追 star=找有狂熱現成社群的 IP 去蹭；我們刻意不蹭走技術深度，star 慢但本就不追 star，印證 `project_devlog_content_roadmap` 路線。
