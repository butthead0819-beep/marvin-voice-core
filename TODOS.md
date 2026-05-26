
## 新功能 — 待完成

### TODO: ⭐ suki DB/JSON 同步斷裂修復（下一輪最先做；taste Phase B2 前置）
**Status:** ✅ DONE（2026-05-22 實作選項 1）。`analyze_daily_log.py` 加 `persist_players_to_db()`，在 json 寫出後把本輪 Gemini 實際更新的 player（`updated_players` keys）用 `MemoryManager.replace_player_memory` 寫進 `marvin.db`；meta 仍寫 json。測試 `tests/test_daily_review_db_sync.py`（4 條：落盤 / 只寫列名 / 保留 meta / 缺名跳過）。**B2 已解鎖。**
**What:** daily review（`scripts/analyze_daily_log.py:1383`）只寫 `suki_memory.json`，但 bot（`MemoryManager._load_all`）只從 `marvin.db` 讀，`_migrate_from_json` 只在 db 空時跑 → **daily review 的 player 分析（likes/impression/relationship）永遠進不了 bot runtime**；bot `_export_json` 還會用 db 覆蓋抹掉 daily 寫的 json。
**證據（2026-05-22 驗證）:** 比對 `records/backups/suki_memory_20260521_121020.json` vs `marvin.db`：大肚 likes daily 寫「與友共飲/駕駛油車」，db 是「飲酒聚會/開燃油車」（不同版本）；impression 64 vs 45 字。daily 5/20–5/21 兩天分析都沒進 db。
**關鍵細節:** bot 對 suki 是**混合讀取**——players 從 db（權威），頂層 meta（marvin_performance/proactive_topics）從 json（`get_meta_state` suki_memory.py:189）。**只有 player 部分斷裂，meta OK**。修復只處理 player 部分。
**順序設計:** json 寫出（含新 meta）在前，db 寫回在後——`replace_player_memory` 的 `_export_json` 保留剛寫的 meta、並把 json player 區段同步成 db repaired 版本 → db/json 最終一致。
**並發殘留（v1 接受）:** daily 12:05 寫 db 時 bot 在跑，若該 player 該時段有語音活動，bot `_save_player` 會用舊 `_cache` 覆蓋。只寫本輪更新者（不碰未出現玩家）已縮小衝突面；白天多數 player 無活動 → 多半保留、重啟後穩定。v2 再做「bot save 前比對 db 版本 / pending queue」徹底解並發。
**Priority:** ~~P1~~ 已完成

---

### TODO: taste 分數分級系統 — Phase B2 / C / D
**Status:** Phase A + B1（commit 0dfd8ca, d099a85）；**B2 ✅ + C ✅ DONE（2026-05-22）**。D 待做。
**What:**
- **B2** ✅：`merge_player` 的 likes/dislikes 改走 taste 加 `_DAILY_TASTE_DELTA=1.5`（新項目只進「曾提及」，跨日累積過 ±3.0 才投影 confirmed），existing confirmed 用 `_build_taste_from_legacy` 保留，結尾 `_project_taste`。端到端驗證：11 個 Gemini likes 一次 → 0 confirmed，解掉「daily 一次加 11 個 likes」。測試 `tests/test_daily_review_taste_merge.py`（8 條）。
- **C** ✅：**改為確定性偵測**（Jack 2026-05-22 拍板，否決原 LLM 即時抽取）。P1 修好同步後 daily 的 LLM 抽取已能進 bot，C 不重做即時 LLM（與 slow-learning 衝突）。新 `taste_extractor.py`：regex 抓明示偏好句「我喜歡/超愛/討厭 X」→ `record_taste_signal(±1.0)` 入曾提及。`VoiceController._record_interest_signals` 接 handle_stt_result 非喚醒路徑（保護 wake 延遲、inline 同 thread 避 sqlite 競態）。隱性興趣仍交 offline daily。測試 `tests/test_taste_extractor.py`(13) + `tests/test_interest_signal_wiring.py`(5)。
- **D**：❌ **否決（2026-05-22）**。「你問我答」校準介面（Jack 確認/否定 → 設高分 / `remove_taste_item`）會新增「持續人類回饋」依賴，與 Jack 硬原則「資料不依賴人類回饋」衝突。盤點確認學習管線已全自動（daily/feedback loop/即時偏好三條都 0 人類；feedback 是 LLM 自動分類非人手按 reaction），唯一人類觸點是 `recall_probe_cases.json` 15 條一次性量測考卷（不餵記憶）。除非 Jack 主動要，不做 D。詳記憶 `feedback_autonomous_learning_no_human_loop`。
**參考:** `suki_memory.py` `LIKE/DISLIKE_THRESHOLD=±3`、`record_taste_signal` / `remove_taste_item` / `_project_taste` / `_build_taste_from_legacy`；記憶 `feedback_dual_path_taste_writes`。
**Priority:** ~~B2 P1 / C P2~~ 已完成 / D P3

---

### TODO: 品質指標 — Phase 2.5 / 4.5
**Status:** P1–4 + cron 已上線（commit eee0c8e, 7e7e5f3, 34a44f7），bot 累積資料中，每日 12:05 出報告 `records/quality_metrics_<date>.md`
**What:**
- **P2.5**：react time 完整端到端——目前量 wake hit→first audio，補一個 utterance-ts→wake-hit 的更早 mark（含 STT/cleaner/pool），看 pool 對反應時間的真實影響。改 `latency_tracker.py` + `discord_voice_engine.py`。
- **P4.5**：RecallHandler 端到端對話 recall（含 LLM 路徑 C），不只 suki 確定性查核。
**先決:** 等幾天 wake→audio baseline 數字出來，看 P2.5 值不值得。recall_probe_cases.json 真實 ground truth 已填 15 條（Jack 確認）。
**Priority:** P2

---

### TODO: suki 雜訊假 player 清理（待 Jack 確認）
**What:** 「測試者/未知人/系統/會別人怎麼講話/修為講話」明顯是 STT 噪音建的假 player（likes 全空）。「狗與鹿」可能是「狗與露」的 STT 變體（兩個都存在、likes 不同）→ 待釐清是否合併。
**How:** suki 加 `delete_player` API 或清理 script，**bot 停止時跑**（避免 `_cache` 覆蓋，像 2026-05-22 移除西藏佛學那樣）。狗與鹿/狗與露 是否同一人要 Jack 確認。
**Priority:** P3（清理，非阻擋）

---

### TODO: IntentBus — Game agent prod wiring（vertical slice 已完成，wiring 未做）
**Status:** DEFERRED（明天工作項 #4，等架構改動沉澱後接）
**What:** voice_controller.py L2400 game cog chain 改成 `IntentContext(mode="game") → bus.dispatch()`，把今天寫的 3 個 game agent（Busted99/Busted/TurtleSoup）真正接到 prod。
**Why:** 今天只完成 vertical slice + 11 tests/agent，bus 還沒在 game mode 跑過 prod 流量。Adversarial review (2026-05-19) 確認：game agents 目前是 dead code in prod，因為 L2402 early return 在 bus.dispatch 之前。
**How to start:** handle_stt_result 把 if game_mode: 那段改成建 IntentContext + bus.dispatch(mode="game")。先量測 bid 對 high-rate STT（10+ utterances/sec 搶答場景）的累積延遲（3 agents × ~10ms = ~30ms/utt 預估）。
**Depends on:** 觀察 1-2 個遊戲 session 確認 bid 延遲不影響搶答體驗。
**Priority:** P2（vertical slice + tests 已驗證，prod 接 wiring 風險中等）。

---

### TODO: IntentBus base.py — re.compile pattern cache
**Status:** DEFERRED（perf 優化，當前 schema 數量未觸發）
**What:** `intent_agents/base.py:101` re.search 每次 bid 重新編譯 pattern。schema 數量大時會超 5ms bid budget。改成 module-level dict cache 或 IntentSchema 加 `_compiled` 屬性。
**Why:** Adversarial review (2026-05-19) 指出。當前 MusicAgentV2 只 7 schemas，未觸發。但若 StatusAgent / VisionAgent / Marmo / PA / Imitation 5 agents 全部加進去 = ~35 schemas/bid，會持續打到 bid budget warning。
**How to start:** IntentSchema 加 `__post_init__` 編譯 patterns；bid() 用 `schema._compiled` 直接 search。
**Priority:** P3（perf，當前未觸發）。

---

### TODO: IntentBus base.py — reason_template ValueError coverage
**Status:** DEFERRED
**What:** `intent_agents/base.py:112` 只 catch KeyError/IndexError。若 reason_template 含 `{slot!r:>10}` 或無效 format spec → ValueError 未 catch → bid 被 bus 的 bare except 吞掉，silent loss。改成 catch (KeyError, IndexError, ValueError)。
**Why:** Adversarial review (2026-05-19) 指出。
**How to start:** 一行改動。加 regression test：reason_template="{x!q}" assert reason fallback 到 schema.name。
**Priority:** P3（trivial fix）。

---

### TODO: CLAUDE.md — 「禁 return None」rule 加註只適用 DeclarativeIntentAgent
**Status:** DEFERRED
**What:** CLAUDE.md「`bid()` 永遠回 Bid 物件，禁 return None」太 absolute——只適用繼承 DeclarativeIntentAgent 的新 agent。v1 MusicAgent / NemoClawAgent / HallucinationGuardAgent 都還是 return None，沒問題。加註避免未來 Claude session 誤改 v1。
**Why:** Adversarial review (2026-05-19) 指出。
**How to start:** 在那條 rule 加「（適用 DeclarativeIntentAgent subclass；v1 legacy agent 不受影響）」。
**Priority:** P3（doc clarity）。

---

### TODO: install-marvin.sh — 預檢 Xcode CLI Tools
**Status:** DEFERRED
**What:** Marvin 多個 Python deps（faster-whisper、numpy wheels）需要 Xcode CLI Tools 編譯。STREAMER_SETUP.md 只在 troubleshooting 提到。install-marvin.sh 應在 Step 1 預先檢查 `xcode-select -p` 並引導安裝。
**Why:** Adversarial review (2026-05-19) 指出。預測 mid-install pip 失敗 + 無 recovery。
**How to start:** 加 `xcode-select -p &>/dev/null || die "請先跑 xcode-select --install 再重試"`。
**Priority:** P2（會擋部分 streamer）。

---

### TODO: error_dispatcher.py — _inflight thread safety
**Status:** DEFERRED（pre-existing module；race 罕見但真實）
**What:** `error_dispatcher.py:103,169,204` `self._inflight` 從 logging thread 寫、從 event loop 讀，無 lock。race 可能丟錯誤或重複 dispatch。
**Why:** Adversarial review (2026-05-19) 指出。改 threading.Lock 或 atomic counter。
**How to start:** 引入 threading.Lock 包住 _inflight 讀寫；或改用 collections.deque 配 atomic-ish ops。
**Priority:** P3（單人 dev 階段罕見觸發）。

---

### TODO: Companion FOLLOWUP_ACTIVE/EXPIRED 事件（Follow-up v2）
**Status:** DEFERRED（v2）
**What:** 在 companion_bridge.py 與 event_protocol.py 加入 `FOLLOWUP_ACTIVE` / `FOLLOWUP_EXPIRED` 事件常數，TTS 問句窗口開啟/到期時廣播給 Companion UI。
**Why:** v1 沒有 UI 倒數計器 chip，事件回路無消費端。Bot 端行為完全不依賴 Companion 知道窗口狀態。v1 diff 縮小，測試集中在 wake_detector + pipeline。
**How to start:** companion_bridge.py 加常數 + emit method；event_protocol.py 對稱常數；app.js 加倒數 chip。
**Depends on:** Follow-up listening v1 上線後確認需要 UI 視覺回饋。

---

### TODO: Follow-up speaker affinity（Jack 的 user_id 優先）
**Status:** DEFERRED（v2，根據 v1 session 資料決定）
**What:** `temporary_open_window(duration, reason, speaker_affinity=None)` — 當指定 speaker_affinity 時，只捕捉該 user_id 的第一語句；其他人的聲音在窗口期間被忽略。
**Why:** first-wins 可能被隊友插話吃掉 Marvin 問 Jack 的問題。v1 接受為群組對話行為，但若實際 session 中誤觸發頻率高，v2 需要 speaker affinity。
**How to start:** 在 handle_stt_result 的 is_open() 判斷處加 speaker 過濾；temporary_open_window 傳入觸發 TTS 的對象 speaker（如能取得）。
**Depends on:** v1 部署後觀察 1 個月誤觸發次數。

---

### TODO: 吧 字尾問句 regex 精實（Follow-up v2）
**Status:** DEFERRED（v2，根據 v1 session 資料決定）
**What:** v1 regex `[?？嗎呢]\s*$` 排除「吧」以降低假陽性。v2 根據 session 日誌評估是否加入 `吧` 並附加形式限制（如 `吧[？?]\s*$` 要求雙重標記）。
**Why:** 「吧」在中文高度 ambiguous（建議詞 vs. 問句），v1 謹慎排除，但會錯過「你合適吧？」等真實問句。
**How to start:** 查 session log 中 Marvin TTS 以「吧」結尾的次數；分類真問句 vs. 建議句，再決定 regex。
**Depends on:** v1 session 日誌（至少 3-5 個 Saturday session）。

---

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

### TODO: ProactiveArbiter — 全遷移 3 個主動發言者進 bidding（CP2 deferred）
**Status:** DEFERRED（2026-05-21 CEO review）
**What:** 把 TopicGenerator social gap / leave_prob 送客 / pending 確認 這 3 個現有主動發言者，全部改成 bid 進 ProactiveArbiter（max wins），取代目前只共用 cooldown 的做法。
**Why:** 收斂「主動發言」這個 latent agent bus，徹底消除互搶麥的 collision class（對上 project_agent_pattern 標的待遷移）。基線只做了 cooldown 防撞，沒收斂邏輯。
**How to start:** 先確認 ProactiveArbiter（基線）上線穩定後，逐一把每個發言者的 `voice_client.play()` 路徑改成 `arbiter.bid()`。一次遷一個 + 測試。
**Depends on:** ProactiveArbiter 基線（本 plan）
**Effort:** M（human ~2-3d / CC ~40min）
**Priority:** P3 — 等 STT/wake/stream 穩定性 incident 平息再動 4 條熱路徑

### TODO: 語音控制同意線 —「別記/別說出去」（CP3 deferred）
**Status:** DEFERRED（2026-05-21 CEO review）
**What:** 新 intent：使用者語音說「別記這個」「這個別說出去」→ 把對應群組記憶標 private 或刪除。把 shareable 同意線從啟發式猜測，升級成使用者可控的信任功能。
**Why:** office-hours：「可分享 vs 私下」這條線劃好本身就是護城河。啟發式預設能擋大部分，但使用者可控才是信任的完整形態。
**How to start:** 加一個 DeclarativeIntentAgent（text pattern：別記/別說/刪掉那個），handler 對 SummaryStore/suki_memory 標 private。先寫紅。
**Depends on:** 返場 callback 上線（先看到真實「講了不該講」案例）
**Effort:** S/M（human ~2d / CC ~30min）
**Priority:** P2

---

### TODO: MemoryCallbackAgent — 升 embedding similarity（D 選項）
**Status:** DEFERRED（等 v1 char-overlap 收 outcome 資料）
**What:** v1 用 unique-char 重疊（沿 speaker_topic_graph.py:227 pattern），對「同意不同詞」無感。升 sentence-transformers cosine 解決。
**Why:** char-overlap 抓不到「我說 AI 他講 model」這類同義詞 callback。但要先看 v1 命中率是否真的太低。
**How to start:** speak_outcome.jsonl 觀察 2 週，若 callback win/天 < 1，加 sentence-transformers 比對。
**Depends on:** MemoryCallbackAgent v1 上線 + outcome 資料。
**Priority:** P3（資料驅動觸發）。

---

### TODO: MemoryCallbackAgent — 升 LLM 關聯判斷（E 選項）
**Status:** DEFERRED
**What:** embedding 仍不夠時，用 gpt-4o-mini 判斷「commitment + 當前 utterance 是否相關」。
**Why:** 最高語意品質但 +500ms-2s latency → 必須非 sync-fast 路徑（pre-compute？背景跑？）+ cost gate。
**How to start:** 等 embedding 也不夠再評估；目前不規劃。
**Depends on:** embedding 升完 + 仍有 quality 缺口。
**Priority:** P3。

---

### TODO: MemoryCallbackAgent — 跨 speaker callback
**Status:** DEFERRED
**What:** v1 只對「commitment 本人在場」bid。未來：Jack 三天前說要試 X、今天 Suki 在場、Suki 講到 X → Marvin 對 Suki 提「Jack 上次也說要試 X」。
**Why:** 社交感最強——「Marvin 把房間裡兩人連起來」是 callback 的高 whoa 變體。但跨 speaker 涉及隱私線（shareable=True 已過濾，但語境延伸需審）。
**How to start:** 加 `MemoryCallbackAgent` mode flag `cross_speaker=True`，bid 時掃所有 present speakers 的 callback queue，handler 措辭模板加「{commit_speaker} 之前說要 X」。
**Depends on:** v1 上線 + 至少 4 週觀察。
**Priority:** P3。

---

### TODO: MemoryCallbackAgent — LLM 措辭潤飾 callback line
**Status:** DEFERRED
**What:** v1 callback 句純模板「對了，你之前說要 X，現在呢？」。未來：根據當前話題 + speaker 情緒 LLM 改寫成「呃 grounded search 那個你後來實際試了嗎」。
**Why:** 模板講多次會像機器人。但 LLM 改寫加 latency + cost，要先看 v1 講多了會不會煩。
**How to start:** speak_outcome 觀察 callback win 之後 30s 內有 STT 比率；< 30% 再考慮潤飾。
**Depends on:** v1 上線 + outcome 觀察。
**Priority:** P3。

---

### TODO: MemoryCallbackAgent — per-commitment 動態 TTL
**Status:** DEFERRED
**What:** 現在 callback_queue 全 7 天 TTL（_CALLBACK_TTL_SECONDS）。「今天買 X」應 1 天、「想學日文」應 30 天。
**Why:** 短期 commitment 過期還在 queue → 假陽性 callback；長期被 TTL 砍掉 → 漏 callback。
**How to start:** commitment 偵測時讓 LLM 額外標 `urgency: short/medium/long`，TTL 對應 1/7/30 天。改 `enqueue_callback` 接 ttl 參數。
**Depends on:** 視 v1 是否真的看到 TTL 失準案例。
**Priority:** P3。

---

### TODO: MemoryCallbackAgent — 升 post_utterance trigger（D7 deferred A）
**Status:** DEFERRED（plan-eng-review 2026-05-26 拍定 v1 走 5s tick）
**What:** v1 走 SpeakBus 既有 5s idle tick → callback latency 0-5s。未來：在 handle_stt_result 末尾加 `await self._speak_bus.tick(SpeakContext(trigger="post_utterance", ...))`，讓 Marvin 0.5s 內接話。
**Why:** 5s latency 跟「Marvin 聰明接話」的瞬時感差距明顯。但加 trigger 要改 SpeakBus.tick + handle_stt_result + recent_utterances 填值，且每句話 +5-15ms STT 路徑 latency。先看 v1 用戶實測感受是否真的「太慢」。
**How to start:** SpeakBus.tick 加 trigger 區分；handle_stt_result 在 cleaner 後叫 tick(trigger="post_utterance")；_build_speak_context 接收 last_text 參數。
**Depends on:** v1 上線 2 週 + Jack 主觀「等太久」回饋。
**Priority:** P3（如真改善 whoa 感則升 P2）。

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
