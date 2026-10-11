
> 2026-10-11 整理：拿掉已完成、前提已經不在（功能被刪、方向換了）、被新設計取代的項目，全部可以從 git 歷史找回。
> 分四區：**接下來做** → **小修** → **條件觸發（等數據，沒數據不動）** → **長期停放**。

## 🔜 接下來做

### TODO: 信任與隱私 — 「馬文忘掉這件事」/「這個別說出去」
**What:** 語音撤回：「馬文忘掉我要去露營這件事」→ 刪除該事件；「這個別說出去」→ 把對應記憶標 private。只能動自己是 owner 的事件/記憶。
**Why:** 虛擬客廳 Phase 1 重點是信任與隱私；目前只能等 30 天自動過期，「可分享 vs 私下」只靠啟發式猜。
**Pros:** 讓人敢在 Marvin 面前聊行程；EventStore 已有刪除基礎。
**Cons:** 多一個寫入意圖，誤觸會刪錯，需確認步驟或只刪最可能的一筆並講出來。
**Context:** 合併舊的兩項：2026-10-10 eng review（D9）不塞進 Phase 1 的語音撤回，與 2026-05-21 CEO review 的「別記/別說」同意線。意圖辨識照 LLM 優先，別只靠 regex。設計見 `docs/designs/event-memory.md`。
**Priority:** P2

### TODO: DJ 口白註冊表第 4 刀 — 依 skip 率砍 mode（10/15）
**What:** 10/15 拿口白 skip 反應訊號（第 1 刀）的資料，砍掉 skip 率明顯偏高的 mode。
**Context:** `docs/designs/dj-narration-mode-registry.md`；記憶 project_dj_narration_mode_registry。
**Priority:** P2（有日期）

### TODO: 移除 select_narration_mode 舊包裝（10/16 起可做）
**What**: `dj_narration_orchestrator.select_narration_mode` 在第 3 刀改成呼叫 `plan_narration` 的薄包裝；穩定後刪掉它，正式呼叫端改用 `plan_narration`，7 個測試檔約 47 處引用改測 `plan_narration`。
**Why**: 2026-10-08 eng review D13 刻意保留舊入口當重構護欄；不清掉會兩個決策入口並存，之後有人從舊入口加邏輯就分岔。
**Cons**: 一次改寫約 47 處測試，要逐一確認斷言語意不變。
**Context**: 第 3 刀 eb01f5c（10/9）已 land，穩定一週＝10/16。設計見 `docs/designs/dj-narration-mode-registry.md`；eng review 報告 `~/.gstack/projects/butthead0819-beep-marvin-voice-core/main-eng-review-20261008-141758.md`（R3/R5）。從 `grep -rn select_narration_mode tests/` 開始。
**Priority:** P3

### TODO: suki 雜訊假 player 清理（待確認）
**What:** `marvin.db` players 表仍有「測試者 / 未知人 / 會別人怎麼講話 / 修為講話」這些 STT 噪音建的假 player（2026-10-11 確認還在）。「狗與露」有兩列，要先確認是不同 guild 還是重複。
**How:** 清理 script，**bot 停止時跑**（避免 `_cache` 覆蓋）；深夜做，不在晚間 20-24。
**Priority:** P3

---

## 🧹 小修（隨手可做）

### TODO: IntentBus base.py — reason_template 漏接 ValueError
**What:** `intent_agents/base.py` 三處 `except (KeyError, IndexError)`（約 :216 / :262 / :296）。reason_template 含無效 format spec → ValueError 未接 → bid 被 bus 的 bare except 吞掉，silent loss。改成也接 ValueError。
**How to start:** 先寫紅：reason_template="{x!q}" 斷言 reason fallback 到 schema.name。
**Priority:** P3

### TODO: IntentBus agents 清掉 voice_controller forwarding stubs
**What**: intent_agents/ 裡還有 9 處 `self.ctrl.X`（stream / radio / _mixer…）走 VC 的轉發 stub，改成直接拿 MusicCog，清完 VC 的轉發層可以刪。
**Start**: `grep -n "self\.ctrl\.\(stream\|radio\|_safe_music\|_mixer\|_cover\|_consecutive\)" intent_agents/`
**Priority:** P3（技術債，不急）

---

## ⏳ 條件觸發（沒數據前不動）

### TODO: audio rescue — readonly_tool_executor 接線
**What:** 給 `AudioRescueAgent` 傳一個 `readonly_tool_executor`，讓 `get_now_playing` / `get_recent_history` 兩個唯讀 tool 真的能執行（現在沒有任何地方傳 → `_execute_readonly` 直接 return，no-op）。
**Cons:** `NowPlayingAgent` 已接「現在放什麼」，重疊度高。
**觸發條件:** replay 資料顯示真有人問這類且其他路徑接不到。
**Priority:** P3

### TODO: audio rescue — 延遲緩解
**What:** 若量到 Gemini 呼叫 p95 > 3s 或「馬文暫停」這類即時指令體感變慢：縮 wav（降取樣 / 剪頭尾靜音）、換更快端點、或加 filler SFX。
**Context:** cleaner 已在 regex race 平行跑，不在關鍵路徑；真正的增量是 Gemini round-trip。
**觸發條件:** p95 數字或使用者抱怨。
**Priority:** P3

### TODO: 真·持續單曲循環模式（loop_on / loop_off）
**What**: `_loop_one` 狀態旗標，`_stream_loop` pop 點在 `_loop_one` 時重用 `_current_stream_info` 不 pop。intent `loop_on`（單曲循環/一直放這首）+ `loop_off`。
**5 個 `_stream_loop` 觸點**（2026-09-10 eng-review 已盤過；行號可能已漂移）:
1. pop 點 → `_loop_one` 時重用 `_current_stream_info`
2. skip / stop / clear_queue 分支 → 清 `_loop_one`
3. DJ 尾段（`_run_tail_dj`）→ `_loop_one` 時不排 `_tail_dj_task`
4. **record_play / stream_history → `_loop_one` 時不重複計播放數**（漏了會洗歪口味統計）
5. autopilot 補位 → `_loop_one` 時 skip
**觸發條件**: daily ritual 再抓到 2+ 次「一直放這首歌」措辭。
**Priority:** P3

### TODO: Follow-up 聆聽窗 — speaker affinity / 「吧」字尾問句
**What:** (a) `wake_detector.temporary_open_window` 加 `speaker_affinity`，只收被問的那個人；(b) `_QUESTION_MARKER_RE`（`wake_detector.py:68`）評估是否加「吧[？?]」。
**觸發條件:** 觀察到隊友插話吃掉 follow-up 窗口，或漏接真問句的案例。
**Priority:** P3

### TODO: PaidUsageGuard 低估 Google Search grounding 成本
**What**: `llm_paid.estimate_cost` 純 token-based；grounding 另外 per-request 計費，所以帶 grounding 的付費呼叫會 under-count。影響 `grounded_qa_agent`、`lyrics_grounded_search`、`game_knowledge_agent`。
**修法**: `estimate_cost` 加 `grounding_request_surcharge` 常數，帶 grounding 的呼叫點傳 flag。
**觸發條件**: grounding 付費用量上來（目前低量，$2/日 cap 遠到不了）。
**Priority:** P3

### TODO: 品質指標 — Phase 2.5 / 4.5
**What:**
- **P2.5**：react time 補 utterance-ts→wake-hit 的更早 mark（含 STT/cleaner/pool），改 `latency_tracker.py`。
- **P4.5**：RecallHandler 端到端對話 recall（含 LLM 路徑 C）。
**觸發條件:** 每日 `records/quality_metrics_<date>.md` 顯示延遲是痛點、或 recall 被抱怨。
**Priority:** P3

---

## 🅿️ 長期停放

### TODO: Claim-level 可反駁解釋機制（推薦引擎 Phase 2）
**What:** 讓使用者對推薦附帶的「解釋本身」按讚/按噓，把解釋存成結構化 claim，讓候選池學到「使用者信任哪種理由」。
**Context:** 設計 `jackhuang-main-design-MusicRecEngine-20260820-114251.md`。DJ 口白已有「X會喜歡這首《Y》，理由是Z」句型，可當 claim 的來源。
**Priority:** P3（長期複利項）

### TODO: Marvin 表演腳本 — 疊加動作指向目前畫面卡片
**What:** HUD 上 Marvin 播放動作時「看向/指向」畫面上實際存在的卡片。
**Context:** 設計 `jackhuang-main-design-MarvinPerformanceScript-20260727-104135.md` Open Question #2。原本寫的 `main_satellite.py` 已不存在，`lastCards` 現在在 `car_http_app.py`，動工前先確認資料源。
**Depends on:** v1 動作庫骨架驗證過。
**Priority:** P3

### TODO: install-marvin.sh — 預檢 Xcode CLI Tools
**What:** Step 1 加 `xcode-select -p &>/dev/null || die "請先跑 xcode-select --install 再重試"`。
**Priority:** P3（等有外部使用者要裝時再做）

### TODO: Docker / Linux path
**What:** Full Docker image；Linux voice path（Whisper-only）需 end-to-end 驗證。
**Priority:** P3（待首個 Linux 用戶）
