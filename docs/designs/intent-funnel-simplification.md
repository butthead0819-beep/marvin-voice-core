# 意圖漏斗簡化：發現 → 確認 → 執行 → 修正 → 滿意

狀態：eng review 完成（2026-10-22）、CEO review 進行中；執行以「## 執行規格定稿（CEO review 後）」為準
範圍：語音 pipeline 骨幹（三條分支共用），`cogs/voice_controller.py` 的 `handle_stt_result` / `_apply_wake_guards` / `_process_queued_query`、`intent_bus.py`、`wake_session.py`、`wake_detector.py`。

## 1. 問題

意圖流程不是缺階段，而是**每個階段散在多處、各做一次**：

| 階段 | 現況 | 問題 |
|---|---|---|
| 發現：算不算對 Marvin 說 | consent → ETD → 幻覺 → IBA 五通道 → 7 道 wake guard → WakeShortcut → 佇列；非喚醒走 followup pending / deferred wake | **6 套接續窗口**各自計時（見 §2） |
| 發現：要做什麼 | IntentBus **前**寫死分支（NemoClaw/Marmo regex、PA 意圖、系統狀態關鍵字）→ IntentBus（J1 regex ∥ J3 cleaner → bid → rescue）→ **後**又有 question opener、music drop、模仿、NemoClaw smart router、gap classifier、presence gate → Marvin LLM | bus 外分支繞過 bid 機制 |
| 確認 | ack 音效、missing_slots 反問、music drop 反問、PA yes/no、低信心只貼文字 | 沒有「何時先確認」的統一規則 |
| 執行 | agent handler + 少數舊 `_handle_*` | 相對乾淨 |
| 修正 | 打斷（10/22）、Frustration / TasteCorrection / SongFeedback、skip | 修正句被當全新一句，不知道上一個意圖（只有 grounded QA 有 qa_turns） |
| 滿意 | judge_outcomes、rescue outcome、agent_gaps、skip、like、frustration 各自寫 | 無法串成一輪漏斗 |

流量（統一區間 2026-10-06～10-22 共 16 天，bot_main/stt_history/bot_stdout 去重，[measured: grep]）：喚醒 555、IntentBus winner 行 412（含 short-circuit 與 bids 兩種行，非唯一回合數）、AmbientQA 行 524、WakeSession 接住 80、WakeShortcut 直派 48、fusion 窗接走 9、`_pending_followups` 接走 8、Music Drop 2、deferred wake 0、**Echo Guard 播放中抑制 380**。
（eng review 更正：原版混用 stt_history 兩個月與 bot_main 兩週的區間。）
**log 看不到觸發**：deferred wake、NemoClaw/Marmo fast-track、模仿、語音系統狀態、PA yes/no、presence gate。看不到 ≠ 零流量：頂層模組（非 `cogs.*`/`intent_agents.*`）的 INFO log 會被 `setup_early_logging` 的 WARNING 等級吞掉（例：`wake_detector` 的「Wake gate open」只記到 1 次、實際接走 9 次）。步驟 2 確認流量時，先確認該路徑的 logger 等級，必要時先加計數再觀察，不能只靠 grep。

## 2. 六套接續窗口

| 窗口 | 位置 | 範圍 | 時長 | 作用 |
|---|---|---|---|---|
| WakeSession | `wake_session.py` | 單一 target | idle 120s | 喚醒後對發起者免喚醒；含打斷（barge-in） |
| fusion 問句窗 | `wake_detector.temporary_open_window` | **全域，不分 speaker** | 呼叫端給 | Marvin 問完問題後免喚醒；60s 內最多 2 次 |
| `_pending_followups` | voice_controller | per speaker | 12s | Marvin 反問（例：哪一首）→ 合成「馬文，播XX」重投 |
| `deferred_wakes` | voice_controller | per speaker | 4s / 2 句 | 低信心喚醒 → 後續像指令就合併觸發 |
| `speaker_dialogue_states` | `_confirmation_flow` | per speaker | 等待逾時 | 只喊「馬文」→ 等後續問句（無 log，但每次短喚醒都會走） |
| `_awaiting_confirmation` | PA | 單一 speaker | — | 記事 yes/no |

六者本質相同：**「Marvin 在等某人的下一句，下一句不需要喚醒詞，並帶著某個期待（問句/歌名/yes-no/任意）」**。

## 3. 目標架構

```
STT ─▶ ① TurnGate（發現：算不算對 Marvin 說）
          每 speaker 一個 ConversationState：
            expecting: none | any | question | song | yes_no
            expires_at, last_intent, qa_turns, barge_in_pending
          六套窗口 → ConversationState.open(expecting=..., ttl=...)
       ─▶ ② IntentBus（發現：要做什麼）
          bus 前後寫死分支 → agent 出價或刪除
       ─▶ ③ ConfirmPolicy（確認）
          高信心 → 直接做｜中 → 邊做邊複述「播《X》」｜低 → 先問（open expecting=yes_no/slot）
       ─▶ ④ handler（執行）
       ─▶ ⑤ 修正：ConversationState.last_intent
          「不是，是 Y」→ 改上一個意圖的 slot 重派，不從頭判
       ─▶ ⑥ TurnOutcome（滿意）
          每輪一筆：turn_id / discovered_by / intent / confirm_tier / executed / corrected / satisfaction_signal
```

原則：骨幹改動、每層靠 Protocol 解耦、優雅降級：守衛類例外 fail-closed（當作回授不觸發），窗口類例外 fail-open（當作沒窗口、要喊馬文）（R4/D8）。

## 4. BDD 用法（行為合約，不導入框架）

- 合併任何窗口前，先把該窗口**現在的行為**寫成 Given/When/Then 情境 → pytest 參數化表格，跑在純核心（ConversationState / TurnGate 決策函式）。合併後同一批情境必須全綠＝characterization 的使用者語言版。
- 情境**取自 stt_history / bot_main 的真實對話**（附原始 log 時戳），不自己想像（Mock 測產品不自己當 fixture）。
- 零流量路徑不寫情境，先問該不該刪。
- 確認、修正兩階段目前沒有統一規則 → 先用情境定規則，再實作。例：
  - Given 中信心點歌 When 「馬文播晴天」 Then 播放且複述「播《晴天》」
  - Given 上一輪播了《X》 When 30s 內「不是，是周杰倫那首」 Then 改 artist slot 重派，不重新喚醒
  - Given fusion 窗開著 When **非提問者**講話 Then 不接走（現況會接走：行為變更，需使用者拍板）

## 5. 遷移順序（一步一 PR，每步可單獨驗證）

1. **TurnOutcome 量測**：只加紀錄、不改行為。每輪一筆 JSONL（`records/turn_outcomes.jsonl`），turn_id 從 STT 帶到 handler。後續每步用它做前後對照（修正率、確認被否定率、窗口接走率）。
2. **確認並刪除零流量路徑**：逐條確認 → 清單給使用者拍板。
3. **六套窗口 → ConversationState**：先寫情境（§4），再逐套搬，一次一套，每套情境全綠才下一套。
4. **bus 外分支收進 IntentBus**。
5. **ConfirmPolicy + 修正接 last_intent**。

## 6. 驗收標準

- 每步：既有測試全綠 + 新情境表全綠 + 棘輪不超（voice_controller.py 只准降）。
- 步驟 1：上線 24h 後 turn_outcomes 覆蓋率 ≥ 95% 的喚醒（對照 `⚡喚醒` 數）。
- 步驟 3：六個窗口相關 self 屬性從 voice_controller.py 移除；WakeSession 接住數、followup 接走數上線前後一週不下降（turn_outcomes 對照）。
- 步驟 5：修正率（corrected / executed）可量出基線後再定目標。

## 7. 未決

- fusion 窗口改成只對 target 開，是行為變更（現況任何人都會被接走）。
- 「中信心邊做邊複述」的信心門檻要從 turn_outcomes 基線定。
- 修正視窗長度（建議沿用 WakeSession idle）。

## Eng Review 記錄

審查目標（固定）：`docs/designs/intent-funnel-simplification.md`（/plan-eng-review，2026-10-22）

### 範圍紀錄（Scope Challenge B）
feature answers: D1=A（步驟 4 bus 外分支收進 IntentBus，這一波做）、D2=A→**D15=A 改為延後**（ConfirmPolicy 移出本波，待 TurnOutcome 一週數據；原推翻 2026-09-25 的決定撤回，decision-log 09355b68 已被取代）、D3=A（修正接 last_intent 這一波做）；structure: A（D4，Original arrangement：新寫 ConversationState / TurnGate / ConfirmPolicy / TurnOutcome 四個模組 + 約 4 個新 agent）；accepted scope: 設計 §5 五步全部、原始檔案排法；pending remedies: none

### Scope Challenge 事實更正
- §1 流量數字改為 10/06–10/22 統一區間（原版混用兩個月與兩週）。證據：bash grep 去重計數。
- §1 補「頂層模組 INFO log 被吞」→ 步驟 2 確認流量不可只靠 grep。證據：`wake_detector` Wake gate open 1 次 vs captured in follow-up 9 次。

## Decision ledger

### R1: 合併後 fusion 問句窗歸誰
Finding: SC3, P1, confidence 9/10, cogs/voice_controller_playback.py:658-665（`if _has_question_marker(text): ... _wd.temporary_open_window(_window, reason="followup")`）+ wake_detector.py:358（單一 `self._open_until`）, reviewer: Claude
Plan baseline: 原提案 §2/§7 列為未決（「fusion 窗口改成只對 target 開，是行為變更」）；TODOS.md:86 已有 speaker_affinity TODO（觸發條件：觀察到隊友插話吃掉窗口）
Runtime evidence: 任何 play_tts 文字帶問句標記就開全域 8s 窗（含主動話題），窗內任何人講話都免喚醒；16 天接走 9 次（WakeSession 同期 80 次）
Comparison grid:
| Choice | Current | A | B | C |
|---|---|---|---|---|
| R1 fusion 窗歸屬 | 全域，任何帶問號的 TTS 開 8s，任何人可接 | Marvin 回答某人→窗口只給那人；主動話題問全房間→開全房間窗 | 全域不變（搬進 ConversationState 的房間層） | 刪除，回應對象追問交給 WakeSession，主動問句不再免喚醒 |
| 範圍 D1–D4 | approved A/A/A/A | 不變 | 不變 | 不變 |
| R2 TurnGate 範圍 | pending | pending | pending | pending |
Question D5:
D5 — 6 套窗口合併後，「Marvin 問完問題的免喚醒窗」要給誰？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）步驟 3 窗口合併。
ELI10: Marvin 講話只要帶問號，就會開一個 8 秒的窗，窗內房間裡任何人講話都不用喊「馬文」。合併成每人一份狀態時要決定這個窗給誰。現況會讓 A 在回答 B 的問題時，旁人的話被當成在回答 Marvin。TODOS 裡本來就有一條要改成只收被問的人。
Stakes if we pick wrong: 維持全域會繼續有旁人插話被接走；直接刪掉則主動話題「你們覺得呢？」大家回答時要重新喊馬文。
Recommendation: A 綁回應對象 because 回答某人時只聽那個人，解掉旁人被接走；主動問全房間時仍保留全房間窗，不傷電台主持人的互動。
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) 綁回應對象 (recommended)
  ✅ 回答某人時只聽那個人，旁人插話不會再被當成在回答 Marvin
  ✅ 主動話題問全房間時仍保留全房間窗，大家回答不用喊馬文
  ❌ 開窗要分「回答某人」和「問全房間」兩種，呼叫端要帶對象資訊
B) 維持全域
  ✅ 行為完全不變，合併只是搬家，回歸風險最低
  ✅ 不用判斷是回答誰，開窗邏輯最簡單
  ❌ 旁人插話被接走的問題原封不動保留，TODOS:86 那條也沒解
C) 刪除 fusion 窗
  ✅ 少一套窗口，回應對象的追問本來就由 WakeSession 接（80 次 vs 9 次）
  ✅ 不會再有任何人因為 Marvin 句尾帶問號而被誤接
  ❌ 主動話題「你們覺得呢？」大家回答時要重新喊馬文，互動感變差
Net: 精準只聽被問的人 vs 零變化 vs 直接少一套。
Header: fusion窗歸屬
Options:
A) 綁回應對象 (recommended)
回答某人時窗口只給那個人，主動話題問全房間才開全房間窗。解掉旁人插話被接走，主動互動保留；代價是要區分「回答某人」和「問全房間」兩種開窗（human: ~1 天 / CC: ~20 分）。
B) 維持全域
照現況搬進 ConversationState 的房間層，任何人可接。行為零變化、風險最低；代價是旁人插話被接走的問題保留（human: ~半天 / CC: ~10 分）。
C) 刪除 fusion 窗
回應對象的追問交給 WakeSession（同期 80 次 vs 9 次），主動問句不再免喚醒。少一套窗口最簡單；代價是主動話題的回答要重新喊馬文（human: ~半天 / CC: ~10 分）。

State: approved
Actual answer: A) 綁回應對象（D5，使用者 2026-10-22）
Accepted scope: 步驟 3 合併時 fusion 問句窗改為：Marvin 回答某人→窗口只給那個人；主動話題問全房間→開全房間窗。開窗呼叫端需帶對象資訊。解掉 TODOS.md:86 speaker_affinity。
History: none

### R2: TurnGate 範圍要不要連 7 道 wake guard 一起收
Finding: SC4, P1, confidence 8/10, cogs/voice_controller.py:1402-1420（`_apply_wake_guards` docstring：「Double Wake / Response Lock / Storm / Echo(+Strong-Voice Bypass) / Global / Follow-up override。回授防護安全核心」）, reviewer: Claude
Plan baseline: 原提案 §3「① TurnGate（發現：算不算對 Marvin 說）…六套窗口 → ConversationState.open」，未寫明 wake guard 是否納入
Runtime evidence: Echo Guard 16 天播放中抑制 380 次（喚醒 555 次）；9/18 曾發生 Echo Guard 吞掉聽糊指令事故（incident_not_listening_echo_diary_resume）
Comparison grid:
| Choice | Current | A | B |
|---|---|---|---|
| R2 TurnGate 範圍 | 未定義（窗口散在 6 處、guard 在 _apply_wake_guards） | 只收 6 套接續窗口；7 道 wake guard 留在 `_apply_wake_guards` 原樣 | 收 6 套窗口 + 7 道 wake guard，「算不算對 Marvin 說」單一負責人 |
| R1 fusion 窗歸屬 | approved A（綁回應對象） | 不變 | 不變 |
| 範圍 D1–D4 | approved A/A/A/A | 不變 | 不變 |
Question D6:
D6 — TurnGate 這一波要不要連 7 道喚醒守衛（Echo Guard 等）一起收進來？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）步驟 3 的 TurnGate 範圍。
ELI10: 「這句算不算對 Marvin 說」現在由兩群東西決定：6 套接續窗口（決定哪些話不用喊馬文），和 7 道喚醒守衛（擋掉回授、重複喚醒、喚醒風暴）。設計文件只講了窗口要合併，沒講守衛。守衛是防止 Marvin 聽到自己聲音的安全核心，16 天擋了 380 次，9/18 也出過吞掉真指令的事故。
Stakes if we pick wrong: 一起收而搬錯一道守衛，Marvin 可能聽到自己的聲音自己觸發、或整晚喚不醒；只收窗口的話，「單一負責人」這一波還沒完全做到。
Recommendation: A 只收窗口 because 守衛是回授防護安全核心、有事故史，跟窗口合併綁在一起會讓出錯時分不出是哪邊；窗口合併穩了再談守衛。
Completeness: A=7/10, B=10/10
Pros / cons:
A) 只收 6 套窗口 (recommended)
  ✅ 守衛一行不動，回授防護不會因為這次重構出事，出問題時範圍清楚
  ✅ 這一波的 BDD 情境只需要涵蓋窗口行為，情境數量可控
  ❌ 「算不算對 Marvin 說」仍分在 TurnGate 和 _apply_wake_guards 兩處
B) 窗口和守衛一起收
  ✅ 發現階段真的只有一個負責人，跟五階段的設計精神完全一致
  ✅ Echo Guard 擋 380 次這類問題之後只要看 TurnGate 一個地方
  ❌ 安全核心跟窗口合併綁在同一批改動，出錯時難分是哪邊壞，回歸面大
Net: 這一波只動窗口、守衛原樣 vs 一次收齊但回歸面加倍。
Header: TurnGate範圍
Options:
A) 只收 6 套窗口 (recommended)
TurnGate 只負責接續窗口，7 道 wake guard 留在 `_apply_wake_guards` 原樣。安全核心不動、出錯範圍清楚；代價是發現階段仍分兩處（human: 不增加 / CC: 不增加）。
B) 窗口和守衛一起收
TurnGate 同時接管 7 道 wake guard。發現階段單一負責人；代價是安全核心一起搬、回歸面加倍，BDD 情境要多涵蓋守衛行為（human: +3 天 / CC: +1 小時）。

State: approved
Actual answer: B) 窗口和守衛一起收（D6，使用者 2026-10-22）
Accepted scope: 步驟 3 的 TurnGate 同時接管 6 套接續窗口與 `_apply_wake_guards` 的 7 道 wake guard（Double Wake / Response Lock / Storm / Echo + Strong-Voice/Intent Bypass / Global / 3s per-speaker / Follow-up override），BDD 情境需涵蓋守衛行為。
History: none

### Section 1 實作補註（已核准範圍的必要實作，不另議）
- 步驟 1 TurnOutcome 的 turn_id 傳遞：沿用 `pipeline_timing` 的轉送方式（ContextVar + producer 在 `_task_data["_timing"]` 塞 snapshot、consumer 還原；pipeline_timing.py:126 註明 ContextVar 不跨 asyncio.Queue，sink 執行緒→loop 的 call_soon_threadsafe 也不帶 context）。
- 步驟 5 ConfirmPolicy 的反問：直接 `ConversationState.open(speaker, expecting=yes_no|slot)`，不經 play_tts 問號觸發的通用窗（D5 已核准窗口綁對象）。

### R3: ConversationState / TurnGate 的執行緒契約
Finding: ARCH-1, P1, confidence 10/10, discord_voice_engine.py:624-625（`if self.on_speech_start_callback: self.on_speech_start_callback(user_id)`，在 `write()` 內）+ venv voice_recv router.py:28/113（`class PacketRouter(threading.Thread)` … `self.sink.write(data.source, data)`）+ cogs/voice_controller.py:1605-1607（`pending.cancel()` 跨執行緒）, reviewer: Claude
Plan baseline: 原提案未提執行緒
Runtime evidence: 10/22 barge-in 在 sink 執行緒 asyncio.create_task → RuntimeError，上線即失效（cc617b1 修）；`pending.cancel()` 現況仍跨執行緒呼叫
Comparison grid:
| Choice | Current | A | B |
|---|---|---|---|
| R3 執行緒契約 | 未定義；sink 執行緒直接呼叫 controller 方法 | ConversationState/TurnGate 只在 event loop 上讀寫；sink 執行緒入口一律經單一 bridge（call_soon_threadsafe）丟回 loop，含既有 pending.cancel() | ConversationState 加 threading.Lock，兩邊都可直接讀寫 |
| R1/R2 | approved A/B | 不變 | 不變 |
| 範圍 D1–D4 | approved A/A/A/A | 不變 | 不變 |
Question D7:
D7 — 合併後的對話狀態要用哪種方式保證跨執行緒安全？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）步驟 3 的執行緒契約。
ELI10: 收音的程式跑在 Discord 收音套件自己的執行緒，其他邏輯跑在主事件迴圈。今天上線的打斷功能就因為在收音執行緒裡直接排工作而完全沒作用。合併後的對話狀態兩邊都會碰到，要先定規則：要嘛只准主迴圈改它（收音執行緒丟訊息過去），要嘛加鎖讓兩邊都能直接改。
Stakes if we pick wrong: 沒定規則，會再出現今天這種上線就默默失效的 bug，或兩個執行緒同時改狀態造成窗口亂開亂關。
Recommendation: A 只在主迴圈讀寫 because 專案其他 async 狀態都住在主迴圈，單一 bridge 好測（今天補的跨執行緒測試就是範本），加鎖則要擔心在主迴圈裡卡鎖。
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) 只在主迴圈讀寫 (recommended)
  ✅ 跟專案其他 async 狀態一致，不用想鎖的順序，邏輯都是單執行緒
  ✅ 收音執行緒只有一個 bridge 入口，跨執行緒測試寫一次就涵蓋
  ❌ 收音執行緒的判斷要多一跳才生效，大約延遲一個事件迴圈週期
B) 加鎖讓兩邊直接改
  ✅ 收音執行緒可以當下就改狀態，不用多一跳
  ✅ 不用改既有呼叫點的結構，直接包鎖
  ❌ 主迴圈裡拿鎖可能卡住整個迴圈，且鎖範圍正是 CLAUDE.md 點名踩過雷的地方
Net: 單執行緒模型多一跳 vs 加鎖即時但有卡迴圈風險。
Header: 執行緒契約
Options:
A) 只在主迴圈讀寫 (recommended)
ConversationState/TurnGate 只在 event loop 上讀寫，sink 執行緒入口一律經單一 bridge（call_soon_threadsafe），既有 pending.cancel() 一併改走 bridge。單執行緒好測；代價是多一個迴圈週期延遲（human: ~半天 / CC: ~15 分）。
B) 加鎖讓兩邊直接改
ConversationState 加 threading.Lock，兩個執行緒都能直接讀寫。即時生效；代價是主迴圈可能卡鎖、鎖範圍是踩過雷的區域（human: ~半天 / CC: ~15 分）。

State: approved
Actual answer: A) 只在主迴圈讀寫（D7，使用者 2026-10-22）
Accepted scope: ConversationState/TurnGate 只在 event loop 上讀寫；sink 執行緒入口一律經單一 bridge（bot.loop.call_soon_threadsafe），既有 handle_raw_speech_start 的 pending.cancel() 一併改走 bridge；補跨執行緒測試。
History: none

### R4: TurnGate 出錯時的降級方向
Finding: ARCH-3, P1, confidence 8/10, 本文件 §3「優雅降級（ConversationState 例外 → 視同無窗口，走正常喚醒）」+ R2 已核准 TurnGate 接管 Echo Guard, reviewer: Claude
Plan baseline: 原提案：例外 → 視同無窗口、走正常喚醒（全部 fail-open）
Runtime evidence: Echo Guard 16 天抑制 380 次；2026-09-16 曾有 Marvin 自我觸發灌爆 voice WS 被 4021 踢線 17 小時的事故（不同根因，但同屬「自己觸發自己」）
Comparison grid:
| Choice | Current | A | B |
|---|---|---|---|
| R4 例外降級 | 全部 fail-open（原提案） | 守衛類 fail-closed（當作回授、不觸發）；窗口類 fail-open（當作沒窗口、要喊馬文） | 全部 fail-open（照原提案） |
| R1/R2/R3 | approved A/B/A | 不變 | 不變 |
Question D8:
D8 — TurnGate 程式出錯時，要往「寧可漏聽」還是「寧可多聽」降級？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）步驟 3 的失敗處理。
ELI10: 原設計寫「TurnGate 出錯就當作沒有窗口，照正常喚醒走」。但 D6 已經讓 TurnGate 接管 Echo Guard 這類擋回授的守衛。如果守衛出錯也照「放行」處理，Marvin 可能聽到自己的聲音自己喚醒自己。比較安全的是分開：守衛出錯就擋（寧可漏聽），窗口出錯就關（要喊馬文才理）。
Stakes if we pick wrong: 全部放行時，一個例外就可能讓 Marvin 自言自語連鎖觸發；全部擋住則一個例外就整晚喚不醒。
Recommendation: A 守衛擋、窗口關 because 回授連鎖是最壞死法，而「要多喊一次馬文」是可以忍受的小代價。
Completeness: A=10/10, B=7/10
Pros / cons:
A) 守衛擋、窗口關 (recommended)
  ✅ 守衛出錯不會讓 Marvin 自己觸發自己，最壞情況只是漏聽一句
  ✅ 窗口出錯退回「要喊馬文」，跟沒有這套系統時一樣，使用者熟悉
  ❌ 要分兩類各自處理例外，每類各要一組失敗測試
B) 全部放行
  ✅ 照原設計，一條規則簡單好懂
  ✅ 出錯時使用者不會覺得 Marvin 沒在聽
  ❌ 守衛出錯時回授防護失效，可能自我觸發連鎖，正是最壞死法
Net: 出錯時寧可漏一句 vs 寧可多觸發。
Header: 失敗降級
Options:
A) 守衛擋、窗口關 (recommended)
守衛類例外 fail-closed（當作回授不觸發），窗口類例外 fail-open（當作沒窗口、要喊馬文），每類各一組失敗測試。最壞只漏一句；代價是兩類分開處理（human: ~半天 / CC: ~15 分）。
B) 全部放行
照原提案，任何例外都當作沒窗口、走正常喚醒。規則單一；代價是守衛出錯時回授防護失效（human: 不增加 / CC: 不增加）。

State: approved
Actual answer: A) 守衛擋、窗口關（D8，使用者 2026-10-22）
Accepted scope: TurnGate 守衛類例外 fail-closed（當作回授、不觸發），窗口類例外 fail-open（當作沒窗口、要喊馬文）；每類各一組失敗測試。§3「優雅降級」敘述改為此規則。
History: none

### R5: 步驟 3 TurnGate 怎麼上線
Finding: ARCH-4, P2, confidence 7/10, 本文件 §5 步驟 3「先寫情境，再逐套搬，一次一套」（無 flag／shadow）；先例：speculative_stt_pipeline 的 J1/J2/J3 shadow 寫 judge_outcomes.jsonl, reviewer: Claude
Plan baseline: 原提案：逐套搬、每套情境全綠才下一套；無 flag、無 shadow
Runtime evidence: R2 已核准 TurnGate 接管安全核心；WakeSession 兩週修 4 輪（7b8f3c1、d831748、5de9538 等）
Comparison grid:
| Choice | Current | A | B | C |
|---|---|---|---|---|
| R5 上線方式 | 無 flag 直接換 | shadow 一週：TurnGate 與舊路徑並跑、只記分歧（records/turn_gate_shadow.jsonl），舊路徑決定行為；分歧審過再切 flag `MARVIN_TURN_GATE=1`，舊路徑保留到穩定一週後刪 | 直接切，但留 flag 可一鍵退回舊路徑 | 直接換、不留 flag（照原提案） |
| R1–R4 | approved A/B/A/A | 不變 | 不變 | 不變 |
Question D9:
D9 — 步驟 3 換上新的 TurnGate 時，要先 shadow 對照一週，還是直接切？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）步驟 3 上線方式。
ELI10: 步驟 3 會一次換掉「算不算對 Marvin 說」的整個判斷，包含擋回授的安全守衛。shadow 的意思是新舊兩套同時跑，但只有舊的真正做決定，新的只把「我會判不一樣」記下來；看一週分歧都合理再切過去。專案裡 Parallel Judges 就是這樣上線的。分歧紀錄本身也是最好的 BDD 情境來源（真實對話、真實分歧）。
Stakes if we pick wrong: 直接切而有一道守衛搬錯，要等使用者抱怨才知道（Marvin 不理人或自己觸發自己）；shadow 則多花一週、過渡期程式碼兩份。
Recommendation: A shadow 一週 because 安全核心加 6 套窗口一起換，回歸面大；shadow 分歧紀錄直接當 BDD 情境，等於免費的真實回歸測試。
Completeness: A=10/10, B=8/10, C=5/10
Pros / cons:
A) shadow 一週再切 (recommended)
  ✅ 切換前就看到新舊判斷的每一筆分歧，守衛搬錯不會傷到使用者
  ✅ 分歧紀錄是真實對話，直接補成 BDD 情境，回歸測試越用越完整
  ❌ 多一週才真正換上，過渡期新舊兩套程式碼同時存在
B) 直接切、留 flag
  ✅ 馬上換上新判斷，flag 一關就退回舊路徑
  ✅ 程式碼只多一個開關，過渡期比 shadow 短
  ❌ 錯誤要等使用者遇到才發現，退回前已經有人被漏聽或誤觸
C) 直接換、不留 flag
  ✅ 最快、程式碼最乾淨，沒有任何過渡期
  ✅ 不用維護舊路徑
  ❌ 出錯只能 revert 重新部署，安全核心出問題時復原最慢
Net: 一週耐心換零傷害切換 vs 立刻換上靠 flag 救火 vs 最快但沒退路。
Header: 上線方式
Options:
A) shadow 一週再切 (recommended)
新舊並跑一週，舊路徑決定行為、TurnGate 只記分歧到 records/turn_gate_shadow.jsonl；分歧審過再開 MARVIN_TURN_GATE=1，舊路徑穩定一週後刪。零傷害切換；代價是多一週、過渡期兩份程式碼（human: +2 天 / CC: +30 分）。
B) 直接切、留 flag
直接換上 TurnGate，保留 MARVIN_TURN_GATE 可一鍵退回舊路徑。上線快、可退回；代價是錯誤要等使用者遇到（human: +半天 / CC: +10 分）。
C) 直接換、不留 flag
照原提案直接換，不留舊路徑。最乾淨；代價是出錯只能 revert 重部署（human: 不增加 / CC: 不增加）。

State: approved
Actual answer: C) 直接換、不留 flag（D9，使用者 2026-10-22）
Accepted scope: 步驟 3 TurnGate 直接取代舊路徑，不留 MARVIN_TURN_GATE flag、不跑 shadow；出錯靠 revert 重部署。shortcut 已記 decision-log ebb45133-0287-4748-b990-163d912cc9cd（ceiling：安全核心出錯只能 revert、錯誤要使用者遇到才發現；upgrade when：上線後出現守衛搬錯或 BDD 漏網回歸 → 補 flag 與舊路徑）。實作時在 TurnGate 切換點標 `gstack-shortcut(dec-ebb45133-0287-4748-b990-163d912cc9cd): ...`。
History: 2026-10-22 經 Outside Voice OV-1 重開，D14=A 取代（3b/3c 各留 kill flag），shortcut 標記不再需要。

### Section 2 實作補註（已核准範圍的必要實作，不另議）
- 步驟 3：`_pending_followups` 與 `deferred_wakes` 目前用合成字串「馬文，{raw}」遞迴重投 `handle_stt_result`（voice_controller.py 約 1352、1374，bypass_etd=True、wake_intent=None）。合併進 ConversationState 後改成 TurnGate 直接判定「接住＋expecting slot」，不再合成喚醒字串重投。

### R6: NemoClaw 快路收進 IntentBus 的方式
Finding: CQ-1, P2, confidence 8/10, cogs/voice_controller.py:299-302（`_NEMOCLAW_RE … (?:open\s*claw|openclaw|claw|龍蝦)`）+ :2926-2931（fast-track，品質閘之前、low_confidence 門檻 0.80）vs intent_agents/nemoclaw_agent.py:18-21（「跟 cogs/voice_controller.py:88 _NEMOCLAW_RE 同步維護」`_LOBSTER_RE = 龍蝦|lobster`）+ :36-53（owner-only、門檻 0.65）；handler voice_controller.py:2744 已強制 owner-only, reviewer: Claude
Plan baseline: D1=A 已核准「bus 外分支收進 IntentBus」；收法未定
Runtime evidence: 兩份 regex 人工同步、已 drift（快路含 openclaw/claw，agent 含 lobster）；兩者實際都只有 owner 能觸發（handler 擋）
Comparison grid:
| Choice | Current | A | B |
|---|---|---|---|
| R6 收法 | 快路 + NemoClawAgent 兩份 regex | 併進現有 NemoClawAgent：regex 取聯集、單一來源；門檻用 agent 的 0.65；單講「龍蝦」仍要觸發（品質閘對 nemoclaw bid 放行），補 BDD 情境；刪 controller 快路與 `_NEMOCLAW_RE` | 新寫 NemoClawFastTrackAgent 照搬快路契約（0.80、品質閘前），與 NemoClawAgent 並存、兩份 regex |
| D1 | approved A | 不變 | 不變 |
Question D10:
D10 — NemoClaw（龍蝦）快路收進 IntentBus 時，要併進現有的 NemoClawAgent，還是另寫一個？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）步驟 4 收編 bus 外分支。
ELI10: 「龍蝦」觸發 NemoClaw 現在有兩套：controller 裡的快路，和 IntentBus 的 NemoClawAgent。兩套各有一份 regex，靠人工同步，已經對不齊（一邊認 openclaw，另一邊認 lobster）。兩套實際上都只有 owner 能觸發，差別只剩門檻和「單講龍蝦也觸發」。併成一個就只剩一份 regex。
Stakes if we pick wrong: 另寫一個會留著兩份 regex 繼續漂移；併的時候沒保留「單講龍蝦」會讓 owner 原本的用法失效。
Recommendation: A 併進現有 agent because 兩者都已經 owner-only，併起來只剩一份 regex，差異（單講龍蝦）用一條 BDD 情境守住即可。
Completeness: A=10/10, B=7/10
Pros / cons:
A) 併進 NemoClawAgent (recommended)
  ✅ 只剩一份 regex，不會再出現一邊認 openclaw、另一邊認 lobster 的漂移
  ✅ 沿用已在 IntentBus 跑的 agent，少寫一個類別、少一組測試
  ❌ 門檻從 0.80 降成 0.65，owner 低信心時會比現在多觸發一點
B) 另寫快路 agent
  ✅ 照搬現有快路行為，門檻和觸發時機零變化
  ✅ 不用改動已經在跑的 NemoClawAgent
  ❌ 兩份 regex 繼續並存、繼續漂移，IntentBus 裡同一件事有兩個出價者
Net: 一份 regex、門檻稍鬆 vs 行為零變化但留著重複。
Header: 龍蝦收編
Options:
A) 併進 NemoClawAgent (recommended)
regex 取聯集成單一來源、門檻用 0.65、單講「龍蝦」仍觸發（補 BDD 情境），刪 controller 快路與 `_NEMOCLAW_RE`。消除重複；代價是低信心門檻變鬆（human: ~半天 / CC: ~15 分）。
B) 另寫快路 agent
新寫 NemoClawFastTrackAgent 照搬快路契約，與 NemoClawAgent 並存。行為零變化；代價是兩份 regex 繼續漂移（human: ~半天 / CC: ~15 分）。

State: approved
Actual answer: A) 併進 NemoClawAgent（D10，使用者 2026-10-22）
Accepted scope: 步驟 4：NemoClaw regex 取聯集（龍蝦/lobster/openclaw/open claw/claw，含前綴詞）成單一來源放 nemoclaw_agent.py；門檻 0.65；單講「龍蝦」仍觸發（品質閘對 nemoclaw bid 放行）並補 BDD 情境；刪 controller 快路（voice_controller.py:2926-2931）與 `_NEMOCLAW_RE`（含 debounced 路徑的引用一併改用 agent 的 regex）。
History: none

### R7: 步驟 3 回歸契約（IRON RULE）
Finding: TEST-1, P1 CRITICAL, confidence 9/10, 既有測試：WakeSession 相關 13 檔 201 個、wake guard 相關 11 檔 142 個、followup/fusion 窗 4 檔 92 個、`_pending_followups` 4 檔 52 個、dialogue state 3 檔 24 個、PA 確認 6 檔 43 個；deferred_wakes 0 個, reviewer: Claude
Plan baseline: §4 BDD 用法（情境取自真實對話、合併前對舊路徑全綠）；§6「既有測試全綠 + 新情境表全綠」
Runtime evidence: WakeSession 兩週修 4 輪；D9=C 不留 flag/shadow → 回歸測試是唯一防線
Preserved behavior: 除下列刻意改變外，6 套窗口與 7 道守衛的現有行為全部保留
Intentional changes (已核准): D5 fusion 窗綁回應對象；D7 sink 執行緒經 bridge；D8 守衛 fail-closed / 窗口 fail-open；D10 NemoClaw 門檻 0.65 + regex 聯集；D2 ConfirmPolicy；D3 修正接 last_intent；步驟 2 刪除的零流量路徑（另議清單）
（2026-10-22 spec review 更新：D2 已由 D15 取代，移出本清單；新增 D14 kill flag 退回行為、CEO-D4（E2「🎧 聽 X 中」顯示）。）
Comparison grid:
| Choice | Current | A | B |
|---|---|---|---|
| R7 回歸覆蓋方式 | 未定 | 既有測試全部保留（只准為上列刻意改變修改，修改處 docstring 註明 D 編號）＋真實情境表：每套窗口、每道守衛各 ≥5 條真實樣本（正反例各半；不足 5 條的用全部），合併前先對舊路徑全綠 | 既有測試改寫到 TurnGate 邊界、舊測試退役＋同 A 的真實情境表 |
| D9 | approved C（無 flag/shadow） | 不變 | 不變 |
Question D11:
D11 — 步驟 3 換掉窗口和守衛時，回歸測試要怎麼保？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）步驟 3 的回歸契約。
ELI10: 你在 D9 選了不留退路直接換，所以測試是唯一防線。現在跟這些機制有關的測試大約有五百多個，是 WakeSession 修 4 輪、Echo Guard 出事故後累積下來的。一種做法是全部保留，只准為已核准的改變去改；另一種是把它們改寫到新的 TurnGate 介面上。兩種都會另外加一張「從真實對話撈出來的情境表」。
Stakes if we pick wrong: 改寫舊測試時漏掉一個邊界情況，就等於把之前修過的 bug 放回來，而且沒有 flag 可以退。
Recommendation: A 保留既有測試 because 舊測試記錄的是真實修過的 bug，保留下來成本最低、最不會漏；只為已核准的改變修改，每處註明 D 編號。
Completeness: A=10/10, B=8/10
Pros / cons:
A) 保留既有測試＋真實情境表 (recommended)
  ✅ 之前修過的每個 bug 都還有測試守著，改寫時漏掉的風險是零
  ✅ 只為已核准的改變修改，每處註明 D 編號，審查時一眼看出是刻意的
  ❌ 舊測試綁在舊方法名上，TurnGate 要保留相容入口或測試要跟著改呼叫點
B) 改寫到 TurnGate 邊界＋真實情境表
  ✅ 測試直接打新介面，之後不用背舊方法名的包袱
  ✅ 可以順便清掉重複或低價值的舊測試
  ❌ 五百多個測試逐一改寫，漏掉一個邊界就等於把修過的 bug 放回來
Net: 舊測試原封守線 vs 改寫乾淨但有漏網風險。
Header: 回歸契約
Options:
A) 保留既有測試＋真實情境表 (recommended)
既有測試全部保留，只准為 D2/D3/D5/D7/D8/D10 與步驟 2 刪除項修改（docstring 註明 D 編號）；另加真實情境表，每套窗口與每道守衛各 ≥5 條真實樣本（不足用全部），合併前先對舊路徑全綠（human: ~3 天 / CC: ~2 小時）。
B) 改寫到 TurnGate 邊界＋真實情境表
既有測試改寫成直接測 TurnGate、舊測試退役；情境表同 A。介面乾淨；代價是五百多個測試改寫有漏網風險（human: ~1 週 / CC: ~4 小時）。

State: approved
Actual answer: B) 改寫到 TurnGate 邊界＋真實情境表（D11，使用者 2026-10-22）
Accepted scope: 步驟 3 回歸契約：既有窗口/守衛相關測試改寫成直接測 TurnGate／ConversationState 介面、舊測試退役（退役時每個需 retirement card，列出被哪個新測試或情境取代）；真實情境表每套窗口與每道守衛各 ≥5 條真實樣本（正反例各半，不足 5 條用全部），合併前先對舊路徑全綠，換上 TurnGate 後同一張表全綠；刻意改變（D2/D3/D5/D7/D8/D10、步驟 2 刪除項）在情境表標註 D 編號並改期望值。
History: none

### Section 4 實作補註（已核准範圍的必要實作，不另議）
- ConfirmPolicy 中信心「邊做邊複述」：複述 TTS 與 handler dispatch 平行（create_task），不得排在 dispatch 之前（延遲是意圖層主要痛點）。

### R8: TurnOutcome 要不要存使用者原文
Finding: PERF-2 / 隱私, P2, confidence 8/10, 本文件 §3「⑥ TurnOutcome…每輪寫一筆：turn_id / discovered_by / intent / confirm_tier / executed / corrected / satisfaction_signal」（未定是否含原文）+ pipeline_timing.py 的 `[STAGE_TIMING] … text='…'` 先例會帶原文, reviewer: Claude
Plan baseline: 欄位清單未列原文，未明說
Runtime evidence: stt_history.log 已存全文；專案有 consent 閘（未同意者不送任何資料）與 zdr_scrub 分層保留 3am job
Comparison grid:
| Choice | Current | A | B |
|---|---|---|---|
| R8 原文 | 未定 | 不存原文：只存 turn_id、speaker、intent/slot 名、各階段結果；要看原文用 turn_id+時間對回 stt_history | 存原文（raw/cleaned），納入 zdr_scrub 分層保留 |
| R1–R7 | approved | 不變 | 不變 |
Question D12:
D12 — 每輪的結果紀錄（TurnOutcome）要不要存使用者講的原文？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）步驟 1 TurnOutcome 欄位。
ELI10: TurnOutcome 每一輪記一筆「怎麼被發現、判成什麼意圖、有沒有確認、做了沒、有沒有被修正」。要決定的是要不要連使用者講的原話一起存。存了分析方便，但多一份原文副本就多一處要做隱私清理；不存的話，要看原話時用時間對回 stt_history。
Stakes if we pick wrong: 存原文卻沒接上 zdr 清理，會留下一份不受保留政策管的對話全文；不存則分析時要多對一次時間。
Recommendation: A 不存原文 because stt_history 已經有全文，再存一份只是多一個隱私面；turn_id 加時間就能對回去。
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) 不存原文 (recommended)
  ✅ 不多出一份對話全文，隱私清理範圍維持現狀
  ✅ 紀錄小、欄位單純，之後統計修正率、確認率不受文字影響
  ❌ 要看某一輪講了什麼，得用 turn_id 和時間對回 stt_history
B) 存原文並納入 zdr 清理
  ✅ 一筆紀錄就看得到原話，分析修正句最方便
  ✅ BDD 情境可以直接從這份紀錄撈，不用跨檔對時間
  ❌ 多一份全文要接 zdr_scrub，漏接就是不受保留政策管的對話副本
Net: 少一份隱私面 vs 分析時少對一次時間。
Header: 原文存否
Options:
A) 不存原文 (recommended)
只存 turn_id、speaker、intent/slot 名與各階段結果；看原文用 turn_id＋時間對回 stt_history。隱私面不變；代價是分析時多對一次（human: 不增加 / CC: 不增加）。
B) 存原文並納入 zdr 清理
存 raw/cleaned 原文，並在 zdr_scrub 加入 turn_outcomes.jsonl 的分層保留。分析方便；代價是多一份全文要清理（human: ~半天 / CC: ~15 分）。

State: approved
Actual answer: A) 不存原文（D12，使用者 2026-10-22）
Accepted scope: TurnOutcome 只存 turn_id、時間、speaker、discovered_by、intent/slot 名、confirm_tier、executed、corrected、satisfaction_signal；不存 raw/cleaned 原文；補測試斷言紀錄不含原文欄位。
History: none

### Outside Voice 事實更正與實作補註（已核准範圍的必要實作，不另議）
來源：Claude Plan 子代理（native fallback，Codex 未安裝），2026-10-22。
- OV-2 一輪的身分：Track A（discord_voice_engine.py:1782）、Track B（:1830）、WakeStream 早期偵測（:941）與 is_wake_check 快照，同一句話最多進來三次。TurnGate 以「speaker + first_audio 時戳」當 turn key 去重；一次性 expectation（yes_no / song）只由該 turn 的第一筆非 wake_check 正式結果消費；TurnOutcome 每個 turn key 只寫一筆。
- OV-3 步驟 3 要在 `IntentContext` 加 `expecting` 欄位（bus 只讀、不改出價邏輯），讓不再重投的接續句帶著期待進 IntentBus。這個最小 API 改動併入步驟 3。
- OV-4 模型補 `RoomState`（房間層級）：Response Lock、Storm、Global 2s、D5 的主動話題房間窗都住這裡；per-speaker 的 ConversationState 只放個人窗口與 last_intent。
- OV-6 D7 範圍釐清：`handle_stt_result` 本來就由 engine 在 loop 上 await（discord_voice_engine.py:941/1782/1830），不加 bridge；bridge 只用在 sink 執行緒的 speech-start 入口（打斷、pending.cancel()）。
- OV-7 `speaker_dialogue_states` 是 `_confirmation_flow` 在 await 的 asyncio.Event（voice_controller.py:2516-2538），攔截點在幻覺守衛之前（:1102 早於 :1119）。改成 ConversationState 時 `_confirmation_flow` 要改寫，並保留攔截順序。
- OV-8 D8 窗口類 fail-open 的意思是「例外時當作沒有窗口、走一般喚醒（要喊馬文）」，不是當作窗口開著。
- OV-9 D5 前置：addressee（回答對象／全房間）要從呼叫端一路傳進 play_tts / SpeakKind，fusion 窗開窗時才知道給誰。
- OV-10 步驟 5 的修正率基線：步驟 5 之前 `corrected` 欄位為空，且 D12 不存原文，基線改用 stt_history 人工標註樣本（步驟 1 上線後抽一週）。
- OV-11 測試數更正：窗口與守衛相關既有測試約 21–32 檔、266–386 個（原寫 500+，是把重疊類別相加）。步驟 3 動工前先產出精確清單，作為 retirement card 的基數。
- OV-13 D10 的門檻 0.80→0.65 是行為改動，單獨一個 commit，commit message 註明。
- OV-14 步驟 1：重投路徑（voice_controller.py:1348/1374）在步驟 3 拿掉之前要以同一個 turn key 去重；WakeShortcut 提前 return（:1276）也要寫 outcome。
- 保留的分歧：OV-12 主張沿用 WakeSession（D4=B）。使用者已在 D4 看過同樣理由後選 A，外部意見沒有帶來新證據，不重開。

### R9: 步驟 3 拆 PR 順序（Outside Voice OV-1）
Finding: OV-1, Critical, confidence 8/10, reviewer: Claude Plan subagent；依據 D9=C（無 flag/shadow）＋D11=B（舊測試改寫退役）同在步驟 3 一個 PR
Plan baseline: §5 步驟 3 一個 PR；D11=B 已核准改寫到 TurnGate 邊界
Runtime evidence: 同一 PR 內改寫測試＋換實作，新測試只能證明新實作自洽；Echo Guard 16 天擋 380 次
Comparison grid:
| Choice | Current | A | B | C | D |
|---|---|---|---|---|---|
| R9 步驟 3 PR 拆法 | 一個 PR（測試改寫＋換實作＋守衛＋窗口） | 拆三個 PR：3a 建 TurnGate 介面（薄轉接到舊實作）、舊測試改打此介面、對舊實作全綠；3b 換窗口實作；3c 換守衛實作 | 維持一個 PR | 先調查 3a 薄轉接是否可行再決定 | 這個提議延後，步驟 3 拆法暫不定 |
| D11 | approved B | 不變（3a 正是 D11 的改寫，只是先對舊實作跑） | 不變 | 不變 | 不變 |
| D9 | approved C | 不變（另議 R10） | 不變 | 不變 | 不變 |
Question D13:
D13 — 步驟 3 要不要拆成「先換測試介面、再換實作、守衛另開」三個 PR？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）步驟 3 的 PR 順序（外部意見 OV-1）。
ELI10: 外部審查指出，步驟 3 現在一個 PR 同時做三件事：把舊測試改寫到新介面、換掉窗口、換掉守衛。測試和實作同時換，新測試只能證明新程式跟自己一致，證明不了「跟以前一樣」。拆開的話，第一個 PR 先做一層薄轉接，讓新介面背後還是舊程式，把測試改過去並跑綠；之後的 PR 才換實作，那時測試就真的是在守行為。
Stakes if we pick wrong: 不拆的話，測試和實作一起換，守衛搬錯不會被任何測試抓到，而且 D9 沒留 flag；拆的話多兩個 PR，照一天 1–2 個的節奏要多一兩天。
Recommendation: A 拆三個 PR because 這是讓 D11 改寫後的測試真正有回歸意義的唯一方式，成本只是多兩個 PR，不改任何已核准的行為。
Completeness: A=10/10, B=6/10, C=8/10, D=6/10
Pros / cons:
A) 拆三個 PR (recommended)
  ✅ 3a 先讓改寫後的測試在舊程式上跑綠，換實作時測試才真的在守行為
  ✅ 守衛單獨一個 PR，Echo Guard 出問題時馬上知道是哪一批改動
  ❌ 多兩個 PR、要寫一層之後會拿掉的薄轉接，照節奏多一兩天
B) 維持一個 PR
  ✅ 一次改完，不用寫過渡用的轉接層
  ✅ PR 數最少，符合一天 1–2 個的節奏
  ❌ 測試和實作一起換，守衛搬錯沒有任何測試抓得到，又沒有 flag
C) 先調查薄轉接可行性
  ✅ 確認舊實作能不能被包成 TurnGate 介面再決定，避免轉接層太複雜
  ✅ 調查只讀程式碼，不改任何東西
  ❌ 拖延決定，調查結果大概率還是要拆
D) 延後這個提議
  ✅ 先做步驟 1、2，等到步驟 3 再決定拆法
  ✅ 不影響其他已核准的決定
  ❌ 步驟 3 的任務檔要等到那時才能定，現在的計畫留一個缺口
Net: 多兩個 PR 換來測試真的有回歸意義 vs 一次改完但守衛沒人守。
Header: 步驟3拆法
Options:
A) 拆三個 PR (recommended)
3a 建 TurnGate 介面（薄轉接到舊實作）、舊測試改打此介面並對舊實作全綠；3b 換窗口實作；3c 換守衛實作。測試真的守行為；代價是多兩個 PR 和一層過渡轉接（human: +2 天 / CC: +30 分）。
B) 維持一個 PR
步驟 3 照原案一個 PR 完成。最省 PR；代價是測試與實作同時換、守衛搬錯無人守（human: 不增加 / CC: 不增加）。
C) 先調查再決定
只讀程式碼確認舊實作能否包成 TurnGate 介面（限 30 分鐘），之後再回來決定拆法；調查期間步驟 3 拆法維持待定（human: ~2 小時 / CC: ~30 分）。
D) 延後這個提議
步驟 3 拆法到動工前再決定，其他決定不變（human: 不增加 / CC: 不增加）。

State: approved
Actual answer: A) 拆三個 PR（D13，使用者 2026-10-22）
Accepted scope: 步驟 3 拆為 3a（建 TurnGate／ConversationState／RoomState 介面，薄轉接到舊實作；既有窗口/守衛測試改寫打此介面，對舊實作全綠；真實情境表對舊實作全綠）→ 3b（換 6 套窗口實作，情境表與改寫後測試全綠）→ 3c（換 7 道守衛實作，同上）。PR 總數由 5 變 7。
History: none

### R10: 重開 D9——要不要至少留一個 kill flag（Outside Voice OV-1 Recommendation）
Finding: OV-1, Critical, confidence 7/10, reviewer: Claude Plan subagent；新風險：D9=C 加 D11=B 讓步驟 3 同時失去 shadow、flag、舊測試三種回歸基準（D13 已補回「舊測試先對舊實作跑綠」這一種）
Plan baseline: D9=C 直接換、不留 flag（shortcut decision-log ebb45133）
Runtime evidence: 3b/3c 換上後若守衛搬錯，現況只能 revert 重部署；bot 24 小時有人在台上
Comparison grid:
| Choice | Current | A | B | C | D |
|---|---|---|---|---|---|
| R10 kill flag | 無（D9=C） | 3b、3c 各留一個 env kill flag（`MARVIN_TURN_GATE_WINDOWS=0`／`MARVIN_TURN_GATE_GUARDS=0` 退回 3a 的薄轉接舊路徑），穩定一週後連同舊路徑刪除；不做 shadow | 維持 D9=C | 先調查 3a 轉接層保留成本再決定 | 這個提議延後 |
| D13 | approved A（拆 3a/3b/3c） | 不變 | 不變 | 不變 | 不變 |
Question D14:
D14 — 既然步驟 3 拆成三個 PR，3b、3c 換上新實作時要不要留一個一鍵退回的開關？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）重開 D9（外部意見 OV-1）。
ELI10: 你在 D9 選了直接換、不留退路。外部審查認為這太冒險。D13 拆 PR 之後，3a 會留下一層「新介面背後還是舊程式」的轉接，所以 3b、3c 換上新實作時，只要留一個環境變數開關，就能一鍵切回 3a 的舊路徑，不用 revert 重部署。不做 shadow，只是保留退路，穩定一週後再把舊路徑和開關一起刪掉。
Stakes if we pick wrong: 不留開關，守衛出錯時要 revert、重跑測試、重新部署，這段時間 Marvin 可能自我觸發或喚不醒；留開關則多維護一週的舊路徑。
Recommendation: A 留 kill flag because D13 的轉接層讓開關幾乎免費，退回只要改一個環境變數，比 revert 重部署快很多。
Completeness: A=10/10, B=5/10, C=8/10, D=5/10
Pros / cons:
A) 3b/3c 各留 kill flag (recommended)
  ✅ 出錯時改一個環境變數重啟就退回舊路徑，不用 revert、重跑測試再部署
  ✅ 用的是 3a 本來就要寫的轉接層，幾乎不多寫程式
  ❌ 舊路徑要多留一週，刪除時要再開一個清理 commit
B) 維持 D9：不留開關
  ✅ 換上就是最終形態，沒有過渡期的分支
  ✅ 程式碼最乾淨，不用記得之後刪開關
  ❌ 安全守衛出錯只能 revert 重部署，復原最慢
C) 先調查轉接層保留成本
  ✅ 確認 3a 轉接層留一週的維護負擔再決定
  ✅ 只讀程式碼，不改任何東西
  ❌ 3a 還沒寫，現在調查只能估計
D) 延後這個提議
  ✅ 等到 3b 動工時再決定要不要留開關
  ✅ 不影響其他已核准的決定
  ❌ 3b 的任務檔要等到那時才能定
Net: 一週的舊路徑換來一鍵退回 vs 程式碼乾淨但只能 revert。
Header: kill flag
Options:
A) 3b/3c 各留 kill flag (recommended)
3b 留 `MARVIN_TURN_GATE_WINDOWS=0`、3c 留 `MARVIN_TURN_GATE_GUARDS=0`，可退回 3a 的薄轉接舊路徑；不做 shadow；穩定一週後連同舊路徑刪除。shortcut ebb45133 視為升級（human: +半天 / CC: +15 分）。
B) 維持 D9：不留開關
照 D9=C 直接換、不留開關。最乾淨；代價是出錯只能 revert 重部署（human: 不增加 / CC: 不增加）。
C) 先調查再決定
只讀程式碼評估 3a 轉接層保留一週的成本（限 30 分鐘），之後再回來決定；期間維持 D9=C（human: ~2 小時 / CC: ~30 分）。
D) 延後這個提議
到 3b 動工前再決定，D9=C 暫時維持（human: 不增加 / CC: 不增加）。

State: approved
Actual answer: A) 3b/3c 各留 kill flag（D14，使用者 2026-10-22）
Accepted scope: 3b 留 `MARVIN_TURN_GATE_WINDOWS`、3c 留 `MARVIN_TURN_GATE_GUARDS`（設 0 退回 3a 薄轉接舊路徑）；不做 shadow；穩定一週後連同舊路徑與開關刪除（清理 commit）。取代 D9=C；decision-log supersede: ebb45133-0287-4748-b990-163d912cc9cd -> 51083c24-2525-4276-8141-e04daccbb814 supersede ebb45133。
History: none

### R11: 重開 D2——ConfirmPolicy 沒有可用的信心來源（Outside Voice OV-5）
Finding: OV-5, High, confidence 8/10, reviewer: Claude Plan subagent；cogs/voice_controller.py:1159（IBA total 是喚醒信心）、IntentBus bid 分數各 agent 自訂未校準、J3 cleaner 另一套
Plan baseline: D2=A ConfirmPolicy 本波做（推翻 2026-09-25 取消低信心確認）；三檔門檻原寫「從 turn_outcomes 基線定」
Runtime evidence: 目前沒有單一「意圖信心」欄位；09-25 取消理由（點歌出錯真因是 autopilot 插隊）未被數據反駁；步驟 1 TurnOutcome 尚未上線
Comparison grid:
| Choice | Current | A | B | C | D |
|---|---|---|---|---|---|
| R11 ConfirmPolicy 時程 | 本波做（D2=A），信心來源未定 | 延到 TurnOutcome 跑滿一週後：先量「判錯後被修正」比率與各 agent bid 分佈，再定信心來源與門檻、決定要不要做；步驟 5 只做修正 | 維持本波做，信心來源先用 winner bid confidence、門檻暫定 0.85/0.60，上線後再校準 | 先調查（限 1 小時）現有 bid/IBA/J3 哪個可當意圖信心，回來再決定時程 | 這個提議延後，D2 暫維持 |
| D3 修正 | approved A | 不變（另議） | 不變 | 不變 | 不變 |
Question D15:
D15 — ConfirmPolicy 要照 D2 這一波就做，還是等 TurnOutcome 有一週數據再定？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）重開 D2（外部意見 OV-5）。
ELI10: ConfirmPolicy 要靠「意圖信心」分成直接做、邊做邊複述、先問三檔。外部審查查了一下，目前沒有一個數字能代表意圖信心：IBA 是「是不是在叫馬文」的信心，IntentBus 的出價分數是各 agent 自己訂的、沒校準。另外 9/25 取消低信心確認的理由（點歌出錯其實是 autopilot 插隊）也還沒有數據推翻。步驟 1 的 TurnOutcome 正好是用來量這些的。
Stakes if we pick wrong: 現在就做，門檻只能先猜，猜錯 Marvin 會常常反問或該問的沒問；延後則這一波「確認」階段仍沒有統一規則。
Recommendation: A 延到 TurnOutcome 有一週數據 because 沒有信心來源，三檔分法只能用猜的；一週後就能看到判錯率和 bid 分佈，再決定要不要推翻 9/25。
Completeness: A=9/10, B=6/10, C=8/10, D=6/10
Pros / cons:
A) 延到 TurnOutcome 一週後 (recommended)
  ✅ 用真實判錯率和 bid 分佈決定信心來源與門檻，不用猜
  ✅ 數據若支持 9/25 的結論，可以直接不做，省下整個元件
  ❌ 這一波結束時，「確認」階段仍然沒有統一規則
B) 維持本波做，先用 bid 分數
  ✅ 五個階段這一波都有對應元件，結構一次到位
  ✅ 先有機制，之後只要調門檻
  ❌ bid 分數各 agent 自訂未校準，暫定門檻可能讓 Marvin 常反問或該問沒問
C) 先調查信心來源（限 1 小時）
  ✅ 確認現有數字裡有沒有能用的，再決定時程
  ✅ 只讀程式碼與 log，不改任何東西
  ❌ 就算找到候選，門檻仍要等數據校準
D) 延後這個提議
  ✅ 先做步驟 1–4，步驟 5 動工前再議
  ✅ 不影響其他已核准的決定
  ❌ 步驟 5 的範圍到時才能定
Net: 等數據定規則 vs 先上機制再校準。
Header: 確認時程
Options:
A) 延到 TurnOutcome 一週後 (recommended)
ConfirmPolicy 從本波移出；TurnOutcome 跑滿一週後量判錯被修正率與各 agent bid 分佈，再定信心來源、門檻、要不要做。步驟 5 只剩修正（human: 減 ~2 天 / CC: 減 ~1 小時）。
B) 維持本波做，先用 bid 分數
照 D2 本波做，信心來源用 winner bid confidence，門檻暫定高 ≥0.85／中 ≥0.60／其餘先問，上線後用 TurnOutcome 校準（human: 不增加 / CC: 不增加）。
C) 先調查再決定
限 1 小時只讀調查 bid／IBA／J3 哪個能當意圖信心，回來再決定時程；期間 D2 維持（human: ~半天 / CC: ~1 小時）。
D) 延後這個提議
到步驟 5 動工前再決定，D2 暫時維持（human: 不增加 / CC: 不增加）。

State: approved
Actual answer: A) 延到 TurnOutcome 一週後（D15，使用者 2026-10-22）
Accepted scope: ConfirmPolicy 移出本波（取代 D2=A）。TurnOutcome 跑滿一週後量「判錯被修正」比率與各 agent bid 分佈，再定意圖信心來源、門檻與是否推翻 2026-09-25 的決定。步驟 5 本波只做「修正接 last_intent」（D3=A）。Section 4「中信心邊做邊複述」補註與 R1 的「ConfirmPolicy 反問開 yes_no 窗」補註隨之暫停。
History: none

### T1: TODO — IntentBus base.py reason_template 漏接 ValueError
Finding: TODO-1, P3, confidence 9/10, TODOS.md:48-51（`intent_agents/base.py` 三處 `except (KeyError, IndexError)`，ValueError 未接 → bid 被 bus 吞掉）, reviewer: Claude
Plan baseline: 既有 TODO，未排入本計畫
Runtime evidence: 步驟 4（D1=A、D10=A）要新增約 4 個 agent，都會經過 base.py 的 reason_template 格式化
Comparison grid:
| Choice | Current | A | B | C |
|---|---|---|---|---|
| T1 | TODOS.md 既有 P3 小修 | 留在 TODOS.md（不動） | 略過 | 併入步驟 4：先寫紅測（reason_template="{x!q}" 斷言 fallback 到 schema.name）再改成也接 ValueError |
Question D16:
D16 — base.py 吞掉 ValueError 的小修，要不要併進步驟 4 一起做？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）TODO 處理。
ELI10: TODOS 裡有一條小修：agent 的理由字串格式寫錯時，會丟出一種沒被接住的錯誤，整個出價被默默吞掉，看起來就像那個 agent 沒出價。步驟 4 要新增好幾個 agent，正好都會經過這段程式。
What: intent_agents/base.py 三處 except 補接 ValueError。Why: 新 agent 的出價被默默吞掉時很難查。Pros: 一行修改、步驟 4 新 agent 直接受保護。Cons: 步驟 4 的 PR 多一個小改動。Context: TODOS.md:48，已有起手測試寫法。Depends on: 無。
Stakes if we pick wrong: 不修的話，步驟 4 新 agent 的理由字串寫錯會讓出價無聲消失，查起來像意圖沒被判到。
Recommendation: C 併入步驟 4 because 步驟 4 就是新增 agent 的那一刀，修正只有一行加一個紅測，順手做最便宜。
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) 留在 TODOS.md
  ✅ 步驟 4 的 PR 範圍維持最小，不夾帶無關修正
  ✅ TODO 本來就寫好起手方式，之後隨時能做
  ❌ 步驟 4 新 agent 上線時仍有出價被默默吞掉的風險
B) 略過
  ✅ 不花任何時間
  ✅ 目前沒有觀察到實際吞掉的案例
  ❌ 問題留著，而且 TODO 也被拿掉，之後沒人記得
C) 併入步驟 4 (recommended)
  ✅ 新 agent 一上線就受保護，出價不會無聲消失
  ✅ 一行修改加一個紅測，成本極低
  ❌ 步驟 4 的 PR 多一個跟收編無直接關係的改動
Net: 順手修掉 vs PR 範圍最乾淨。
Header: ValueError修
Options:
A) 留在 TODOS.md
維持現狀，TODO 不動（human: 不增加 / CC: 不增加）。
B) 略過
從考慮中拿掉，TODOS.md 也不保留（human: 不增加 / CC: 不增加）。
C) 併入步驟 4 (recommended)
步驟 4 先寫紅測再補接 ValueError，完成後刪除 TODOS.md:48 這條（human: ~30 分 / CC: ~5 分）。

State: approved
Actual answer: C) 併入步驟 4（D16，使用者 2026-10-22）
Accepted scope: 步驟 4 先寫紅測（reason_template="{x!q}" 斷言 reason fallback 到 schema.name），再把 intent_agents/base.py 三處 except 補接 ValueError；完成後刪除 TODOS.md:48 這條。
History: none

### T2: TODO — ConfirmPolicy 回頭評估（D15 延後項）
Finding: TODO-2, P2, confidence 9/10, R11/D15（ConfirmPolicy 延到 TurnOutcome 一週後）, reviewer: Claude
Plan baseline: D15=A 已核准延後，但沒有放進任何追蹤清單
Runtime evidence: TODOS.md 目前沒有這條；步驟 1 上線日未定
Comparison grid:
| Choice | Current | A | B | C |
|---|---|---|---|---|
| T2 | 只記在本文件 ledger | 加進 TODOS.md：觸發條件「步驟 1 TurnOutcome 上線滿 7 天」，內容含要量的兩個數字與 09-25 決定 | 略過，只留在本文件 | 本波就做（等於推翻 D15） |
Question D17:
D17 — ConfirmPolicy「一週後再評估」要不要寫進 TODOS.md 追蹤？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）TODO 處理。
ELI10: 剛才 D15 決定 ConfirmPolicy 等 TurnOutcome 有一週數據再說，但這件事現在只寫在設計文件的審查紀錄裡，沒放進每天會看的 TODOS.md，一週後很可能沒人記得。
What: TODOS.md 新增「ConfirmPolicy 回頭評估」。Why: 延後的決定要有觸發點才會回來。Pros: 一週後自然浮出來，帶著要量的數字。Cons: TODOS 多一條。Context: 要量「判錯被修正」比率與各 agent bid 分佈，決定是否推翻 2026-09-25 取消低信心確認。Depends on: 步驟 1 TurnOutcome 上線滿 7 天。
Stakes if we pick wrong: 不寫進去，「確認」階段可能就此沒人回頭處理，五階段永遠缺一塊。
Recommendation: A 加進 TODOS.md because 延後的決定要掛在每日看的清單上，才不會變成「延後」等於「取消」。
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) 加進 TODOS.md (recommended)
  ✅ 有明確觸發條件，步驟 1 滿一週就會浮出來
  ✅ 寫明要量的數字，到時不用重新想要看什麼
  ❌ TODOS.md 多一條，要記得觸發後回來處理
B) 略過，只留在本文件
  ✅ TODOS.md 不增加項目
  ✅ 設計文件裡已有完整紀錄
  ❌ 設計文件不是每天會看的地方，延後很可能變成被遺忘
C) 本波就做
  ✅ 不用等，這一波五階段都有元件
  ✅ 不用多追蹤一條 TODO
  ❌ 等於推翻剛做的 D15，信心來源仍然沒有
Net: 掛在每日清單上 vs 只留在設計文件。
Header: 確認回頭看
Options:
A) 加進 TODOS.md (recommended)
新增 TODO：觸發條件「步驟 1 TurnOutcome 上線滿 7 天」，量判錯被修正比率與各 agent bid 分佈，決定是否做 ConfirmPolicy、是否推翻 2026-09-25（human: ~5 分 / CC: ~1 分）。
B) 略過，只留在本文件
不寫進 TODOS.md（human: 不增加 / CC: 不增加）。
C) 本波就做
ConfirmPolicy 回到本波（推翻 D15）（human: +2 天 / CC: +1 小時）。

State: approved
Actual answer: A) 加進 TODOS.md（D17，使用者 2026-10-22）
Accepted scope: TODOS.md 新增「ConfirmPolicy 回頭評估」：觸發條件「步驟 1 TurnOutcome 上線滿 7 天」；量判錯被修正比率與各 agent bid 分佈；決定是否做 ConfirmPolicy、是否推翻 2026-09-25。
History: none

### T3: TODO — Echo Guard 播放中抑制 380 次抽樣稽核
Finding: TODO-3, P2, confidence 7/10, cogs/voice_controller.py:1456-1487（`⏭️ [Echo Guard] … 抑制來自 {speaker} 的可能回授觸發`）；16 天 380 次 vs 喚醒 555 次；2026-10-21 00:42 實例「跳過他跳過他不要碰過他」是真人遊戲對話被標成回授, reviewer: Claude
Plan baseline: 本計畫 3c 會搬 Echo Guard，但只保證「行為不變」，不檢討它擋得對不對
Runtime evidence: 9/18 曾有 Echo Guard 吞掉聽糊指令的事故（incident_not_listening_echo_diary_resume）
Comparison grid:
| Choice | Current | A | B | C |
|---|---|---|---|---|
| T3 | 未追蹤 | 加進 TODOS.md：3c 上線後抽樣 50 筆被擋的句子，分「真回授／真人閒聊／真人對 Marvin 的指令」三類，指令類 >10% 才開修 | 略過 | 併入 3a 的真實情境表一起做（抽樣同時當情境來源） |
Question D18:
D18 — Echo Guard 16 天擋了 380 次，要不要追蹤一次「擋得對不對」的抽樣稽核？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）TODO 處理。
ELI10: Echo Guard 在 Marvin 播放時會擋掉疑似「聽到自己聲音」的喚醒，16 天擋了 380 次，跟真正喚醒的 555 次差不多多。昨晚就有一筆是朋友在玩遊戲講話被當成回授擋掉。這次重構只保證搬家後行為一樣，不會檢查它原本擋得對不對；9/18 也發生過它吞掉真指令的事故。
What: 抽樣 50 筆被擋的句子分類。Why: 如果有一成以上是真人對 Marvin 講話被吞，就是「Marvin 不理人」的來源。Pros: 用一次抽樣決定要不要修，不用猜。Cons: 人工分類約半小時。Context: 3c 搬完守衛後做，避免重構與修正混在一起。Depends on: 3c 上線（或併入 3a 情境表）。
Stakes if we pick wrong: 不追蹤，若真有大量指令被吞，使用者會一直覺得 Marvin 沒在聽，卻沒人知道原因。
Recommendation: C 併入 3a 情境表 because 3a 本來就要從真實紀錄撈 Echo Guard 的樣本，同一次抽樣順便分類，不用另排時間。
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) 加進 TODOS.md（3c 後做）
  ✅ 重構與檢討分開，3c 不會夾帶行為修正
  ✅ 有明確的修正門檻（指令類 >10%），不會無止盡調參
  ❌ 要另外排時間抽樣，跟 3a 撈情境重複工作
B) 略過
  ✅ 不花時間
  ✅ 守衛維持現狀，不冒新的回授風險
  ❌ 如果真有指令被吞，「Marvin 不理人」的來源一直查不到
C) 併入 3a 情境表 (recommended)
  ✅ 3a 撈 Echo Guard 情境時同一批樣本順便分類，不重複工作
  ✅ 分類結果直接變成情境表的正反例，回歸測試更完整
  ❌ 3a 的範圍多一點，若分類發現問題也要忍到 3c 之後才修
Net: 順手在 3a 分類 vs 另排時間 vs 不管。
Header: EchoGuard稽核
Options:
A) 加進 TODOS.md（3c 後做）
新增 TODO：3c 上線後抽樣 50 筆被擋句子三分類，指令類 >10% 才開修（human: ~5 分 / CC: ~1 分）。
B) 略過
不追蹤（human: 不增加 / CC: 不增加）。
C) 併入 3a 情境表 (recommended)
3a 撈 Echo Guard 情境時抽樣 50 筆三分類，結果入情境表；若指令類 >10%，於 3c 之後另開修正（不在本波修）（human: +30 分 / CC: +10 分）。

State: approved
Actual answer: C) 併入 3a 情境表（D18，使用者 2026-10-22）
Accepted scope: 3a 撈 Echo Guard 真實情境時抽樣 50 筆被擋句子，分「真回授／真人閒聊／真人對 Marvin 的指令」三類，結果入情境表（正反例）；若指令類 >10%，3c 之後另開修正，不在本波修。
History: none

Approval readiness: PASS — R1(D5) R2(D6) R3(D7) R4(D8) R5(D9，已被 R10/D14 取代) R6(D10) R7(D11) R8(D12) R9(D13) R10(D14) R11(D15) T1(D16) T2(D17) T3(D18)；範圍 D1/D3/D4 見範圍紀錄，D2 已被 D15 取代。

## 審查核准版計畫（取代 §3 / §5 的執行順序；§1–§7 原文保留供對照）
> 已由「## 執行規格定稿（CEO review 後）」取代，保留供對照。

```
STT（engine 在 event loop 上 await；Track A/B/WakeStream/wake_check 以 speaker+first_audio 去重成一個 turn）
 │
 ├─ sink 執行緒 speech-start ──bridge(call_soon_threadsafe)──┐   （D7：只有這個入口需要 bridge）
 ▼                                                          ▼
① TurnGate ── RoomState（Response Lock / Storm / Global 2s / 主動話題房間窗）
 │          └ ConversationState × speaker（WakeSession / D5 綁對象的問句窗 / 反問接續 / dialogue 等問句 / PA yes-no / barge_in_pending / last_intent）
 │          └ 7 道守衛（D6）；例外：守衛 fail-closed、窗口 fail-open＝當作沒窗口（D8）
 ▼
② IntentBus（IntentContext.expecting；NemoClaw 快路併進 NemoClawAgent（D10）；Marmo/PA/狀態/模仿/music drop 收成 agent（D1）；base.py 補接 ValueError（D16））
 ▼
④ handler
 ▼
⑤ 修正：ConversationState.last_intent，「不是，是 Y」→ _resolve_and_redispatch 改 slot（D3）
 ▼
⑥ TurnOutcome（每 turn key 一筆，不存原文（D12）；turn_id 沿用 pipeline_timing 的 _task_data["_timing"] 轉送）
（③ ConfirmPolicy 延後，待 TurnOutcome 一週數據（D15、TODOS.md））
```

PR 順序（一天 1–2 個，共 7 個）：
1. **TurnOutcome**：只加紀錄；turn key 去重（Track A/B/WakeStream/wake_check、重投路徑、WakeShortcut 提前 return 也要寫）。預留 gate decision 欄位（CEO E1）；scripts/daily_user_needs.py 加漏斗段（CEO E3）。
2. **刪除零流量路徑**：逐條確認 logger 等級後再看流量（頂層模組 INFO 會被吞）；清單給使用者拍板。deferred wake（16 天 0 筆、0 個測試）為首位候選。
3a. **TurnGate／ConversationState／RoomState 介面＋薄轉接到舊實作**：既有窗口/守衛測試改寫打新介面、對舊實作全綠；真實情境表（每窗口/守衛 ≥5 條，含 Echo Guard 50 筆三分類（D18））對舊實作全綠；`IntentContext.expecting` 欄位；TurnGate 每次接／不接寫 gate decision（CEO E1）。
3b. **換 6 套窗口實作**：D5 綁對象（addressee 傳進 play_tts）、dialogue state 的 asyncio.Event 改寫並保留攔截順序、不再合成「馬文，…」重投；kill flag `MARVIN_TURN_GATE_WINDOWS`（D14）；開窗時文字頻道「🎧 聽 X 中」、關窗編輯同一則（CEO E2）。
3c. **換 7 道守衛實作**：kill flag `MARVIN_TURN_GATE_GUARDS`（D14）；pending.cancel() 改走 bridge（D7）。
4. **bus 外分支收進 IntentBus**：NemoClaw 併入（門檻 0.65 單獨一個 commit）、Marmo/PA/狀態/模仿/music drop agent、base.py ValueError（D16）。
5. **修正接 last_intent**：基線用 stt_history 人工標註一週樣本（OV-10）。
（3b/3c 穩定一週後：清理 commit 刪舊路徑與兩個 kill flag。）

## NOT in scope
- ConfirmPolicy（確認三檔）：延後到 TurnOutcome 一週數據（D15，TODOS.md 追蹤）。
- Echo Guard 擋太多的修正：本波只抽樣分類（D18），指令類 >10% 才在 3c 之後另開。
- `_QUESTION_MARKER_RE` 加「吧」字尾：TODOS.md:86 (b) 保留，不在本波。
- IntentBus forwarding stubs 清理（TODOS.md:53）：與本計畫無依賴。
- shadow 對照：D14 只留 kill flag，不做 shadow。

## What already exists
- WakeSession（wake_session.py）：已是對發起者免喚醒的純核心，含 barge-in（10/22）。D4=A 決定新寫 ConversationState 取代，WakeSession 的測試依 D11=B 改寫退役。
- pipeline_timing（ContextVar + `_task_data["_timing"]` 跨佇列轉送 + jsonl）：TurnOutcome 沿用其轉送方式（不沿用模組）。
- IntentBus `_resolve_and_redispatch` + missing_slots：修正接 last_intent 直接沿用。
- NemoClawAgent：快路併入（D10）。
- tts_speak_policy（純函式裁決）：ConfirmPolicy 日後若做，可比照這個純函式模式。
- 跨執行緒測試範本：tests/test_barge_in_wiring.py::test_gate_called_from_sink_thread_schedules_watch_on_loop（cc617b1）。

## Failure modes
> 已由「## 執行規格定稿（CEO review 後）」取代，保留供對照。
| 新路徑 | 實際會發生的失敗 | 測試 / 錯誤處理 | 使用者看到的 |
|---|---|---|---|
| sink 執行緒入口 | 在非 loop 執行緒建 task → RuntimeError，功能默默失效（10/22 真實發生） | D7 bridge + 跨執行緒測試 | 修好前：完全靜默（已修 cc617b1） |
| TurnGate 守衛 | 例外 | D8 fail-closed + 失敗測試 | 漏聽一句，要再喊一次 |
| TurnGate 窗口 | 例外 | D8 fail-open（當作沒窗口）+ 失敗測試 | 要喊馬文，跟沒窗口時一樣 |
| Track A/B 重複進入 | 一次性 expectation 被 A 消費、B 被當新句 | OV-2 turn key 去重 + 情境 | 修好前：反問被吃掉或答兩次 |
| 3b/3c 換上後搬錯 | 自我觸發或喚不醒 | 3a 舊測試先對舊實作綠 + 情境表 + kill flag | 改 env 重啟即退回 |
| TurnOutcome 寫檔 | 磁碟/IO 例外 | 吞例外 + 測試 | 無（只少一筆紀錄） |
| NemoClaw 門檻放寬 | owner 低信心時多觸發 | 單獨 commit + 情境「單講龍蝦」 | 偶爾多一次 NemoClaw 回應 |
Critical gaps（無測試＋無處理＋靜默）：0。

## Worktree parallelization strategy
> 已由「## 執行規格定稿（CEO review 後）」取代，保留供對照。
| Step | Modules touched | Depends on |
|---|---|---|
| 1 TurnOutcome | pipeline/、cogs/voice_controller* | — |
| 2 刪零流量 | cogs/voice_controller* | 1（用 TurnOutcome 確認流量） |
| 3a 介面＋測試 | wake/、cogs/voice_controller*、tests/ | 2 |
| 3b 窗口 | wake/、cogs/voice_controller*、playback | 3a |
| 3c 守衛 | wake/、cogs/voice_controller* | 3b |
| 4 bus 收編 | intent_agents/、intent_bus、cogs/voice_controller* | 3a（expecting 欄位） |
| 5 修正 | wake/、intent_bus | 3b、4 |
Parallel lanes：Lane A：1 → 2 → 3a → 3b → 3c → 5（共用 cogs/voice_controller*）；Lane B：4 的新 agent 檔（intent_agents/）可在 3a 合併後與 3b 平行撰寫，接線（_process_queued_query）等 3c 合併後再做。
Execution order：A 依序；3a 合併後開 B 寫 agent；3c 合併後合 B 的接線，再做 5。
Conflict flags：cogs/voice_controller.py 與其 mixin 是共用熱點（棘輪只准降），B 的接線必須排在 A 之後。

## Implementation Tasks
> 已由「## 執行規格定稿（CEO review 後）」取代，保留供對照。
- [ ] **T1 (P1, human: ~1 天 / CC: ~30 分)** — TurnOutcome — 每 turn key 一筆、不存原文
  - Surfaced by: Section 1 ARCH-2、Outside Voice OV-2/OV-14、R8(D12)
  - Files: 新 turn_outcome 模組、cogs/voice_controller.py、discord_voice_engine.py（turn key）
  - Verify: pytest 傳遞/去重/不含原文/寫檔失敗不拋；上線 24h 覆蓋 ≥95% 喚醒
- [ ] **T2 (P1, human: ~半天 / CC: ~20 分)** — 零流量路徑 — 確認 logger 等級後量流量、列刪除清單
  - Surfaced by: Scope Challenge SC2、Test review（deferred wake 0 測試 0 流量）
  - Files: cogs/voice_controller.py、wake_detector.py
  - Verify: 清單附每條的流量證據；使用者拍板後刪除，全套 pytest 綠
- [ ] **T3 (P1, human: ~3 天 / CC: ~2 小時)** — 3a TurnGate 介面＋薄轉接＋測試改寫＋真實情境表
  - Surfaced by: R7(D11)、R9(D13)、OV-3/OV-4/OV-11、T3(D18)
  - Files: 新 conversation_state / turn_gate 模組、intent_bus.py（expecting）、tests/
  - Verify: 改寫後測試與情境表對舊實作全綠；精確退役清單＋retirement card
- [ ] **T4 (P1, human: ~2 天 / CC: ~1 小時)** — 3b 換 6 套窗口
  - Surfaced by: R1(D5)、OV-7/OV-9、Section 2 CQ-2、R10(D14)
  - Files: turn_gate/conversation_state、cogs/voice_controller_playback.py（addressee）、cogs/voice_controller.py
  - Verify: 情境表＋改寫後測試全綠；`MARVIN_TURN_GATE_WINDOWS=0` 退回舊路徑的測試
- [ ] **T5 (P1, human: ~2 天 / CC: ~1 小時)** — 3c 換 7 道守衛
  - Surfaced by: R2(D6)、R3(D7)、R4(D8)、R10(D14)
  - Files: turn_gate、cogs/voice_controller.py、cogs/voice_controller_wake_session.py
  - Verify: 守衛 fail-closed / 窗口 fail-open 失敗測試；跨執行緒 pending.cancel 測試；`MARVIN_TURN_GATE_GUARDS=0` 退回測試
- [ ] **T6 (P2, human: ~2 天 / CC: ~1 小時)** — bus 收編
  - Surfaced by: D1、R6(D10)、T1(D16)、OV-13
  - Files: intent_agents/nemoclaw_agent.py、新 agent 檔、intent_agents/base.py、cogs/voice_controller.py
  - Verify: 「單講龍蝦」情境、regex 聯集表、ValueError 紅測；門檻改動單獨 commit
- [ ] **T7 (P2, human: ~1 天 / CC: ~30 分)** — 修正接 last_intent
  - Surfaced by: D3、OV-10
  - Files: conversation_state、intent_bus.py
  - Verify: 「不是，是 Y」重派測試、無 last_intent 走一般路徑；基線用人工標註樣本
- [ ] **T8 (P3, human: ~1 小時 / CC: ~10 分)** — 3b/3c 穩定一週後刪舊路徑與 kill flag
  - Surfaced by: R10(D14)
  - Files: turn_gate、cogs/voice_controller*
  - Verify: 全套 pytest 綠、棘輪下降

## Unresolved decisions
無。

## REALITY rows
- REALITY: premise-table finding unverified=5（NemoClaw/Marmo fast-track、模仿、語音系統狀態、PA yes/no、presence gate 的流量只靠 grep，頂層 logger 會吞 INFO） wake_detector.py:386
- REALITY: already-done pass origin/main 自 2026-10-22 起無命名檔案或符號命中 intent_bus.py:266
- REALITY: surface-check pass 無 CLI；既有 env `MARVIN_FOLLOWUP_WINDOW_SEC`、`MARVIN_GAP_CLASSIFIER` 已核對，新 env 由本計畫定義 cogs/voice_controller_playback.py:664
- REALITY: binding-decisions finding D2 重新引入 2026-09-25 已否決的低信心確認，經 OV-5 後 D15 延後（decision-log acba7d9a） cogs/voice_controller.py:1159
- REALITY: numbers finding 原 §1 流量混用兩個區間、測試數 500+ 估錯，已改為 [measured: grep 10/06–10/22] 與 266–386 cogs/voice_controller.py:1456
- REALITY: deferred-asks pass 無連結 issue docs/designs/intent-funnel-simplification.md:1
- REALITY: pr-count finding 預設一波一 PR；本計畫 5→7 個 PR（D13），照一天 1–2 個約 4–7 天，PR 之間無跨 PR 的使用者可見引用 docs/designs/intent-funnel-simplification.md:5

## Completion summary
- Step 0: Scope Challenge — scope reduced per recommendation（D15 把 ConfirmPolicy 移出本波；D6 擴大守衛範圍）
- Reality rows: 7/7 applicable rows emitted, 5 unverified premises
- Architecture Review: 5 issues found（執行緒契約、失敗降級、上線方式、turn_id 轉送、反問窗口）
- Code Quality Review: 2 issues found（NemoClaw 重複 regex、合成字串重投）
- Test Review: diagram produced, 12 gaps identified（回歸契約 R7 + 情境/失敗/跨執行緒等）
- Performance Review: 2 issues found（複述須平行、TurnOutcome 原文）
- NOT in scope: written
- What already exists: written
- TODOS.md updates: 3 items proposed to user（D16 併入步驟 4、D17 加入、D18 併入 3a）＋TODOS.md:86 (a) 依 D5 註記
- Failure modes: 0 critical gaps flagged
- Unresolved decisions: 0 in this review
- Outside voice: codex 未安裝 → native Claude Plan subagent completed（outside coverage unavailable），14 條發現：4 條重開決策（D13、D14、D15 採納，D4 保留分歧）、10 條轉為事實更正與實作補註
- Parallelization: 2 lanes, 1 parallel / 6 sequential
- Lake Score: 6/9（D4、D6、D8、D10、D13、D14 選 10/10；D9、D11、D15 未選 10/10）
- 審查中另發現並修復 P0：barge-in 在 sink 執行緒 create_task 失效（cc617b1，已部署）

## Suppressed findings
- (confidence 4/10) 確認句延遲：barge-in 確認句走 `_shared_tier_router.quick`，免費池冷卻時可能逾時 3s 而略過——屬已上線功能的觀察項，非本計畫範圍。

## CEO Review 記錄（/plan-ceo-review，2026-10-22）

### 0A 前提挑戰
- 真正的問題（使用者說的）：showay 10/13「如果馬文可以順利聊天會很贊，我真的是這樣覺得」。北極星是「房間有人時數」（記憶 project_marvin_job_and_north_star）。
- 計畫解的問題：意圖流程散在多處（工程問題）。跟「順利聊天」相關的只有窗口合併（誰不用喊馬文、Marvin 在聽誰）。
- 使用者痛點證據（10/08–10/21，scripts/daily_user_needs.py --days 14）：pragmatic negative 0 筆；未滿足需求 38 筆，其中 18 筆 UNKNOWN 是 STT 糊字，其餘多是閒聊被誤判；對 Marvin 直接抱怨的話 16 天 1 句（還是使用者自己講錯）。
- 已知最大的體感問題是對談延遲：入隊→主 LLM p50 6.1s ＝ IntentBus 1.2s ＋ AudioRescue 2.9s ＋ gap classifier 1.7s（記憶 project_fast_followup_routing，n=24）；TODOS.md 的 P1 就是「對談模式極速低延遲通道」。
- 不做的代價：短期使用者感受不到差別；長期是每加一個窗口或意圖都要在 6 處判斷，且 10/22 已實證會出「上線就默默失效」的 bug。
- 結論：計畫解的是代理問題（程式結構）。窗口合併對「順利聊天」有間接幫助，守衛搬家、bus 收編則跟使用者感受無直接關係。

### 0B 既有程式可沿用
| 子問題 | 既有程式 | 本計畫（eng review 核准版）處理 |
|---|---|---|
| 誰不用喊馬文 | WakeSession（wake_session.py）、fusion 窗、4 套其他窗口 | 新寫 ConversationState／RoomState（D4=A 不沿用 WakeSession） |
| 每輪量測 | pipeline_timing（ContextVar＋跨佇列轉送＋jsonl） | 新寫 TurnOutcome，沿用轉送方式 |
| 修正 | IntentBus `_resolve_and_redispatch` | 沿用 |
| 對談延遲 | 10/20 已有分支 feat/fast-followup-routing（對談窗跳 rescue、gap classifier 預設關，PR#139 待 merge） | 本計畫沒有涵蓋 |

### 0C 理想狀態
```
  現在                               這個計畫                          12 個月後的理想
  6 套窗口各自判斷誰不用喊馬文；     統一的 TurnGate＋每輪紀錄；       在客廳裡跟朋友一樣自然插話：
  對談一輪主 LLM 前要等 ~6s；        守衛與 bus 也重整；               Marvin 知道誰在跟它講話（不用喊名字）、
  修正要整句重講                     「不是，是 Y」能改                 回話不到 1 秒、聽錯一句話就能改
```
這個計畫往理想前進了「知道誰在跟它講話」和「能改」兩塊，但最直接的「回話快」不在範圍內。

### REALITY rows（CEO）
- REALITY: premise-table finding unverified=2（「計畫能改善順利聊天」無量測；「修正句常見」無數據） docs/designs/intent-funnel-simplification.md:3
- REALITY: already-done finding 對談延遲的一部分已在 feat/fast-followup-routing（2f6ca41、PR#139）做了，本計畫未引用 TODOS.md:7
- REALITY: surface-check pass 無 CLI/路由；env 名已在 eng review 核對 cogs/voice_controller_playback.py:664
- REALITY: binding-decisions pass 09-25 低信心確認已由 D15 守住；北極星「房間有人時數」不被違反 TODOS.md:7
- REALITY: numbers pass 16 天流量 [measured: grep]、延遲 6.1s [measured: log n=24，10/20] docs/designs/intent-funnel-simplification.md:19
- REALITY: deferred-asks pass 無連結 issue docs/designs/intent-funnel-simplification.md:1
- REALITY: pr-count finding eng 核准版 7 個 PR，照一天 1–2 個約 4–7 天，期間 P1 延遲工作排不進來 TODOS.md:11

### CEO decision ledger
| ID and owner | Contract and evidence | Current | Proposed | Status | Exact approval and scope |
|---|---|---|---|---|---|
| C1 方向（使用者） | 0A：使用者痛點是順利聊天＋延遲；計畫解結構問題 | eng review 核准版（7 PR：1 → 2 → 3a → 3b → 3c → 4 → 5） | A 維持；B 只做 1、2 其餘看數據；C 以「順利聊天」重排 | approved | CEO-D1=A（使用者 2026-10-22）：照工程版 7 個 PR 全做，順序不變；對談延遲（TODOS P1）不併入本計畫 |
| E1 addressee 資料（使用者） | 0C 理想「不用喊名字」；TurnGate 是唯一判斷點；D12 不存原文；用途（spec review 修正）：守衛／窗口稽核＋E4 觸發計數；記的是系統判斷不是正確答案，要當訓練資料須另做人工標註對回 stt_history | TurnOutcome 只記最終結果 | 每次接／不接判斷（含被守衛擋、被窗口接走、沒接）都寫一筆 gate decision（理由代碼、窗口種類、信心），不存原文 | approved | CEO-D3=A（使用者 2026-10-22）：步驟 1 TurnOutcome 預留 gate decision 欄位，3a 起 TurnGate 每次接／不接寫一筆（理由代碼、窗口種類、信心），不存原文 |
| E2 「在聽誰」提示（使用者） | 使用者常不確定要不要喊馬文；已有文字頻道鏡像與 HUD | 只有開窗提示音 | ConversationState 開/關窗時在文字頻道（既有鏡像通道）顯示 `🎧 聽 X 中` 並在關窗時更新 | approved | CEO-D4=A（使用者 2026-10-22）：3b 起開窗發「🎧 聽 X 中」，關窗編輯同一則為已結束，不另發新訊息 |
| E3 每日漏斗報表（使用者） | daily ritual 已有 scripts/daily_user_needs.py | 無漏斗數字 | daily_user_needs.py 加一段讀 turn_outcomes.jsonl：喚醒數、執行率、修正率、守衛擋下數 | approved | CEO-D5=A（使用者 2026-10-22）：步驟 1 一併在 scripts/daily_user_needs.py 加漏斗段（喚醒數、執行率、修正率、守衛擋下數）並補測試 |
| E4 多人同時對談（使用者） | ConversationState 每人一份；WakeSession 目前單一 target；10/17 群聊誤接事故 | 同時只有一個 target | 最多 N=2 人同時有自己的對談窗 | deferred | CEO-D6=B（使用者 2026-10-22）：寫進 TODOS.md，觸發條件「E1 紀錄一週內『窗口被他人搶走』≥5 次」 |
| E5 修正延伸成撤銷（使用者） | D3 修正接 last_intent；「重播X＝點歌X」等既有語意 | 只改 slot 重派 | 「剛剛那個不對／取消」→ 撤回上一個可撤回動作（點歌移出佇列、音量回復） | deferred | CEO-D7=B（使用者 2026-10-22）：寫進 TODOS.md，觸發條件「E3 報表兩週內修正 ≥10 次且含撤回語意」 |
| S1 E1 紀錄位置（使用者） | spec review 完整性 1／範圍 2／一致性 6：OV-2「每 turn key 一筆」與 E1「每次判斷一筆」衝突；步驟 1 預留欄位無值 | CEO-D3：步驟 1 TurnOutcome 預留欄位，3a 起寫 | A 3a 起獨立 turn_gate_decisions.jsonl、步驟 1 不預留；B 照 CEO-D3；C 步驟 1 就把現有守衛擋下結果寫進獨立檔 | approved | CEO-D8=C（使用者 2026-10-22）：取代 CEO-D3 的「步驟 1 預留 TurnOutcome 欄位」。records/turn_gate_decisions.jsonl 每次判斷一筆（turn_key、track、理由代碼、窗口種類、IBA 信心或 null），統計以 turn_key 去重；步驟 1 在 `_apply_wake_guards` 擋下時寫（守衛名稱當理由代碼），3a 起由 TurnGate 寫完整理由（時間點經 spec review 第 2 輪修正為三階段分工，見定稿補充第 2 輪） |
| S2 E2 顯示哪些窗口（使用者） | spec review 完整性 5：8s 問句窗/12s 反問窗/PA 都發會刷頻 | CEO-D4：開/關窗都顯示（未限定窗口種類） | A 只顯示對談窗（WakeSession 類）與主動話題房間窗；B 所有窗口都顯示 | approved | CEO-D9=A（使用者 2026-10-22）：只有 WakeSession 類對談窗與 D5 房間窗顯示（房間窗文案「🎧 聽大家回答中」），關窗編輯同一則；Discord 發送／編輯失敗吞掉記 log；`MARVIN_TURN_GATE_WINDOWS=0` 時不顯示；關窗由 loop 上的計時器觸發（D7） |
| S3 D15 量得到的資料（使用者） | spec review 完整性 4：D12 欄位無 bid、corrected 步驟 5 前為空 | T2：觸發「步驟 1 上線滿 7 天」，量修正率與 bid 分佈 | A TurnOutcome 加 winner_agent/winner_bid，觸發改「步驟 5 上線滿 7 天」；B 只加欄位、觸發不變、修正率用人工標註；C 維持原樣 | approved | CEO-D10=A（使用者 2026-10-22）：TurnOutcome 欄位白名單加 winner_agent、winner_bid（D12 仍不存原文）；TODOS.md ConfirmPolicy 回頭評估觸發改「步驟 5 上線滿 7 天」 |
| S4 PR#139 合併順序（使用者） | spec review 可行性 1：PR#139 動對談窗與 gap classifier，跟 3b、步驟 4 重疊 | 未定 | A 步驟 1 之前合併 PR#139，3a 情境表納入其行為；B 凍結 PR#139 到本計畫完成後再議；C 本計畫完成後重做 PR#139 | approved | CEO-D11=A（使用者 2026-10-22）：步驟 1 之前先合 PR#139（含 #140 疊加變更）進 main；本計畫各 PR 以合併後 main 為基礎；3a 情境表納入「對談窗內跳過 rescue」行為 |
| S5 修正規則（使用者） | spec review 完整性 7：§7「修正視窗長度」未決但寫無未決 | 未定（§7 建議沿用 WakeSession idle） | A last_intent 存活 = WakeSession idle 120s、只認同一 speaker、需在對談窗內或喊馬文；B 30 秒、只認同一 speaker、需喊馬文 | approved | CEO-D12=A（使用者 2026-10-22）：last_intent 存活 120 秒；只認同一 speaker；對談窗內免喊、窗外要喊馬文；`MARVIN_TURN_GATE_WINDOWS=0` 時修正關閉走一般路徑；解掉 §7「修正視窗長度」 |
| S6 3a 拆分（使用者） | spec review 可行性 3：3a 含 266–386 測試改寫＋三介面＋情境表＋50 筆人工分類，CC 2 小時不實際 | D13：3a 一個 PR | A 拆 3a-1（精確清單＋介面＋薄轉接＋守衛回傳理由代碼）與 3a-2（測試改寫＋情境表＋人工分類），PR 7→8；B 維持一個 PR，只修正估時 | approved | CEO-D13=A（使用者 2026-10-22）：3a 拆 3a-1／3a-2，本計畫 PR 7→8（另有 PR#139 先合） |
| S7 守衛例外告警（使用者） | spec review 第 3 輪範圍 4-1：D8 只核准 fail-closed＋失敗測試 | 未核准（補充第 14 條） | A 同一守衛連續 3 次例外 → logger.error 經既有 ErrorDispatcher 寫 incident＋DM owner；B 只寫 guard_exception 代碼、不告警 | approved | CEO-D14=A（使用者 2026-10-22）：同一守衛連續 3 次例外 → logger.error 經既有 ErrorDispatcher 寫 incident 並 DM owner；3c 驗收含測試 |
| S8 紀錄檔保留期限（使用者） | spec review 第 3 輪範圍 4-2：D12 未核准保留期限 | 未定 | A turn_outcomes／turn_gate_decisions 保留 30 天，併入 3am maintenance；B 不設期限，之後再議（列 TODO） | approved | CEO-D15=A（使用者 2026-10-22）：兩檔保留 30 天，於 scripts/run_maintenance.py 3am 作業清理並補測試 |
| V1 Response Lock 解鎖（使用者） | CEO Outside Voice 1，已核對：`_wake_response_pending=False` 只在 :1439 逾時與 :2946；1b790f9 刪掉 TTS 送達解鎖；16 天 Response Lock 壓抑 66 次 | 現行：接受喚醒後鎖到 30 秒逾時 | A 步驟 1 前獨立小 PR 補回「TTS 送達即解鎖」（含紅測），R7 情境表以修正後為準；B 維持現行並凍結進情境表；C 先調查；D 延後 | approved | CEO-D17=A（使用者 2026-10-22）：步驟 0 之後、步驟 1 之前獨立小 PR 補回 TTS 送達即解除 Response Lock（先寫重現紅測）；部署後觀察 Response Lock 壓抑次數與回授誤觸；R7 情境表以修正後為準 |
| V2 TTS 回授冷卻（使用者） | 同上：`_tts_echo_cooldown_until` 只初始化（:473），16 天「TTS冷卻」0 次 | 現行：冷卻從不生效 | A 同一小 PR 補回 TTS 結束後 2 秒冷卻（含紅測）；B 刪掉這條死碼分支（步驟 2）；C 先調查；D 延後 | approved | CEO-D18=B（使用者 2026-10-22「都照你的建議，我同意」）：步驟 2 刪除 `_tts_echo_cooldown_until` 欄位與 Echo Guard 冷卻分支，情境表不收 |
| V3 步驟 5 需求門檻（使用者） | CEO Outside Voice 4：修正句頻率從未量過 | D3=A：步驟 5 本波做，無門檻 | A 步驟 5 開工前先人工抽樣，一週修正句 ≥5 次才做，否則延後；B 維持 D3；C 先調查；D 延後此提議 | approved | CEO-D19=A（使用者 2026-10-22「都照你的建議，我同意」）：步驟 5 開工前以 stt_history 人工抽樣一週，「不是，是 Y」類修正句 ≥5 次才做；不足則步驟 5 移入 TODOS.md（觸發：抽樣達標），D15 觸發隨之改為「步驟 5 上線滿 7 天或步驟 5 確定延後」 |
| V4 E2 訊息量（使用者） | CEO Outside Voice 9：實際每天 15–20 次開窗＝30–40 則 send/edit | CEO-D4/D9：每次開窗發新訊息、關窗編輯 | A 改為一則固定狀態訊息，只編輯不新發（「🎧 目前在聽：X」／「🎧 沒有在聽」）；B 維持；C 先調查；D 延後 E2 | approved | CEO-D20=A（使用者 2026-10-22「都照你的建議，我同意」）：E2 改為 `active_text_channel` 一則固定狀態訊息，bot 啟動時發一次（或沿用上次 id 失敗就重發），之後只編輯：「🎧 目前在聽：X」／「🎧 聽大家回答中」／「🎧 沒有在聽」；取代 CEO-D4/D9 的「每次開窗發新訊息、關窗編輯」，CEO-D9 的窗口範圍（對談窗＋房間窗）不變 |
| V5 kill flag 組合（使用者） | CEO Outside Voice 10＋2：混合組合最易出錯、只存在約一週 | D14：兩個獨立 flag、四組合都支援 | A 只支援 (1,1)、(0,0)，混合組合啟動時拒絕並記 error；B 維持四組合；C 先調查；D 延後 | approved | CEO-D21=A（使用者 2026-10-22「都照你的建議，我同意」）：kill flag 只支援兩個值相同的組合；混合組合啟動時 logger.error 並以 (0,0) 舊路徑運行；G8 表只保留 (1,1)、(0,0) 兩列；取代 D14 的四組合 |

## answered decision (C1)
Commitment comparison:
| Commitment | Source/approval or pending | Current | A | B | C |
|---|---|---|---|---|---|
| 步驟 1 TurnOutcome | eng D12 等 | 做 | 做 | 做 | 做 |
| 步驟 2 刪零流量 | eng 範圍 | 做 | 做 | 做 | 做 |
| 3a/3b 介面＋窗口合併 | eng D5/D11/D13/D14 | 做 | 做 | 等數據 | 做 |
| 對談窗快速通道（TODOS P1） | 未納入 | 不做 | 不做 | 不做 | 3b 後接著做（另開設計，不在本次核准細節） |
| 3c 守衛搬家 | eng D6/D8/D14 | 做 | 做 | 等數據 | 等 TurnOutcome 數據 |
| 4 bus 收編 | eng D1/D10/D16 | 做 | 做 | 等數據 | 等 TurnOutcome 數據 |
| 5 修正接 last_intent | eng D3 | 做 | 做 | 等數據 | 等 TurnOutcome 數據 |

Question: CEO-D1 — C1: 這波意圖重構要照工程版全做，還是改成以「順利聊天」為主軸重新排序？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）CEO 審查方向。
ELI10: 使用者實際說出口的願望是「馬文可以順利聊天」，最大的體感問題是對談一輪要等約 6 秒。工程審查核准的 7 個 PR 主要在整理程式結構：其中窗口合併（誰不用喊馬文）對聊天有幫助，但守衛搬家、bus 收編、修正機制跟使用者感受沒有直接關係，而且 2 週內對 Marvin 的不滿是 0 筆。可以照原案全做、只做量測和刪除等數據，或是保留跟聊天有關的部分、接著做延遲，其餘看數據。
Stakes if we pick wrong: 照原案全做，接下來 4–7 天都在重構，使用者感受不到改變、P1 延遲排不進來；砍太多則窗口散落的問題繼續留著。
Recommendation: C 以順利聊天重排 because 窗口合併和延遲都直接對應使用者說的「順利聊天」，守衛和 bus 整理等 TurnOutcome 數據證明有需要再做；短期多花的只是排序，長期架構不變。
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) 照工程版全做
  ✅ 結構一次整理到位，eng review 的決定全部落地
  ✅ 之後新增窗口或意圖只要改一個地方
  ❌ 4–7 天沒有使用者看得到的改變，P1 對談延遲被往後推
B) 只做 1、2，其餘看數據
  ✅ 風險最低，數據決定接下來要不要做
  ✅ 最快騰出時間做 P1 延遲
  ❌ 窗口散落、Marvin 聽誰不清楚的問題暫時不動
C) 以順利聊天重排 (recommended)
  ✅ 每一步都對應使用者說的「順利聊天」：先知道在聽誰，再讓回話變快
  ✅ 守衛安全核心不用急著搬，回授風險往後延到有數據時
  ❌ 結構整理只完成一半，3c/4/5 可能拖很久才做
Net: 結構一次到位 vs 最小風險等數據 vs 先做使用者感受得到的那一半。
Header: 重構方向
A) 照工程版全做
7 個 PR 依序做完（effort L、risk medium）。✅ 結構一次整理到位，eng review 的決定全部落地 ✅ 之後新增窗口或意圖只要改一個地方 ❌ 4–7 天沒有使用者看得到的改變，P1 對談延遲被往後推
B) 只做 1、2，其餘看數據
只做 TurnOutcome 和刪零流量路徑，3–5 等一週數據再排（effort S、risk low）。✅ 風險最低，數據決定接下來要不要做 ✅ 最快騰出時間做 P1 延遲 ❌ 窗口散落、Marvin 聽誰不清楚的問題暫時不動
C) 以順利聊天重排 (recommended)
1 → 2 → 3a → 3b，接著做對談窗快速通道（TODOS P1，另開設計），3c/4/5 等 TurnOutcome 數據再排（effort M、risk medium）。✅ 每一步都對應使用者說的「順利聊天」：先知道在聽誰，再讓回話變快 ✅ 守衛安全核心不用急著搬，回授風險往後延到有數據時 ❌ 結構整理只完成一半，3c/4/5 可能拖很久才做

## answered decision (E1)
Commitment comparison:
| Commitment | Source/approval or pending | Current | A | B | C |
|---|---|---|---|---|---|
| gate decision 紀錄 | pending | 無 | 加進本計畫（步驟 1 預留欄位，3a 起 TurnGate 寫入） | 寫進 TODOS.md 延後 | 不做 |
| 不存原文 | D12 approved | 不存 | 不存 | 不存 | 不存 |

Question: CEO-D3 — E1: 要不要讓 TurnGate 的每一次「接／不接」判斷都留紀錄，當作之後學「是不是對 Marvin 說」的資料？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）SCOPE EXPANSION 候選。
ELI10: TurnGate 是整個系統唯一判斷「這句是不是在跟 Marvin 講話」的地方。計畫目前只記最後結果（有沒有執行）。如果把每次判斷的理由也記下來（被守衛擋、被哪個窗口接走、沒接），之後就能抽樣看哪裡判錯，甚至訓練一個不用喊名字也能判斷的模型。照 D12，一樣不存原文。
Stakes if we pick wrong: 不記的話，之後想改善「不用喊名字」只能重新埋點、再等數據；記了則每輪多幾個欄位。
Recommendation: A 加進本計畫 because TurnGate 跟 TurnOutcome 本來就在這波新寫，多記幾個欄位幾乎免費，卻是往「不用喊名字」最重要的資料基礎。
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) 加進本計畫 (recommended)
  ✅ 每次判斷都有理由代碼，抽樣就看得出守衛或窗口哪裡判錯
  ✅ 累積成之後訓練「不用喊名字」判斷模型的標注資料
  ❌ 每輪紀錄多幾個欄位，TurnOutcome 的測試要多涵蓋這些欄位
B) 延後到 TODOS.md
  ✅ 這波範圍不變，先把結構做完
  ✅ 之後有明確需求時再加也不難
  ❌ 晚加就晚累積資料，等要用時又得再等幾週
C) 不做
  ✅ 紀錄最精簡
  ✅ 不多花任何時間
  ❌ 「是不是對 Marvin 說」的判斷永遠只能靠人工翻 log
Net: 現在順手記下判斷理由 vs 之後再補。
Header: 判斷紀錄
A) 加進本計畫 (recommended)
步驟 1 的 TurnOutcome 預留 gate decision 欄位，3a 起 TurnGate 每次接／不接都寫一筆（理由代碼、窗口種類、信心），不存原文（effort S、risk low）。✅ 抽樣就看得出哪裡判錯 ✅ 累積成未來模型的標注資料 ❌ TurnOutcome 測試要多涵蓋欄位
B) 延後到 TODOS.md
寫進 TODOS.md，觸發條件「步驟 3c 上線後」（effort S、risk low）。✅ 這波範圍不變 ✅ 之後補也不難 ❌ 資料晚累積
C) 不做
不記判斷理由（effort S、零實作、risk low）。✅ 紀錄最精簡 ✅ 不花時間 ❌ 判斷品質只能人工翻 log

## answered decision (E2)
Commitment comparison:
| Commitment | Source/approval or pending | Current | A | B | C |
|---|---|---|---|---|---|
| 「在聽誰」文字提示 | pending | 只有開窗提示音 | 加進本計畫（3b 起，開/關窗時在文字頻道顯示並更新同一則訊息） | 寫進 TODOS.md 延後 | 不做 |
| 開窗提示音 | 既有 | 保留 | 保留 | 保留 | 保留 |

Question: CEO-D4 — E2: 要不要在文字頻道顯示「Marvin 正在聽誰」？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）SCOPE EXPANSION 候選。
ELI10: 現在只有一聲提示音告訴大家 Marvin 開始聽某個人了，其他人不知道它在聽誰、還要聽多久，常常不確定要不要喊「馬文」。ConversationState 合併後，系統第一次能精確知道「現在誰的窗口開著、還剩幾秒」。把這個顯示在文字頻道（例如「🎧 聽 showay 中」），關窗時更新成已結束，大家就知道什麼時候不用喊名字。
Stakes if we pick wrong: 不做的話，窗口合併的好處使用者看不見，還是會多喊或少喊馬文；做了則文字頻道多一則會更新的訊息。
Recommendation: A 加進本計畫 because 這是讓使用者「感受得到」窗口合併的最便宜方式，一則訊息就把隱形的狀態變成看得見的。
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) 加進本計畫 (recommended)
  ✅ 房間裡每個人都看得到 Marvin 在聽誰，知道什麼時候不用喊名字
  ✅ 窗口合併的成果第一次變成使用者看得到的改變
  ❌ 文字頻道多一則會更新的訊息，要注意不要刷頻（只更新同一則）
B) 延後到 TODOS.md
  ✅ 這波範圍不變
  ✅ 等 3b 穩定後再加，狀態來源更可靠
  ❌ 3b 上線後一段時間，窗口合併仍然是使用者看不見的改變
C) 不做
  ✅ 文字頻道維持乾淨
  ✅ 不多花任何時間
  ❌ 使用者繼續猜 Marvin 有沒有在聽，多喊或漏喊馬文
Net: 讓窗口狀態看得見 vs 文字頻道維持乾淨。
Header: 在聽誰提示
A) 加進本計畫 (recommended)
3b 起 ConversationState 開窗時在文字頻道（既有鏡像通道）發「🎧 聽 X 中」，關窗時編輯同一則改為已結束，不刷新訊息（effort S、risk low）。✅ 大家看得到在聽誰 ✅ 窗口合併變得看得見 ❌ 多一則會更新的訊息
B) 延後到 TODOS.md
寫進 TODOS.md，觸發條件「3b 穩定一週」（effort S、risk low）。✅ 範圍不變 ✅ 狀態來源更穩 ❌ 改變暫時看不見
C) 不做
不顯示（effort S、零實作、risk low）。✅ 頻道乾淨 ✅ 不花時間 ❌ 使用者繼續猜

## answered decision (E3)
Commitment comparison:
| Commitment | Source/approval or pending | Current | A | B | C |
|---|---|---|---|---|---|
| 每日漏斗報表 | pending | 無 | 加進本計畫（步驟 1 一併，daily_user_needs.py 加一段） | 寫進 TODOS.md 延後 | 不做 |
| TurnOutcome 紀錄 | 步驟 1 approved | 有 | 有 | 有 | 有 |

Question: CEO-D5 — E3: 要不要把漏斗數字（喚醒、執行、修正、被擋）加進每天起手看的需求報表？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）SCOPE EXPANSION 候選。
ELI10: 步驟 1 會開始記每一輪的結果，但如果沒有人每天看，數據就只是躺在檔案裡。你每天起手會跑 daily_user_needs.py 看昨天的需求和抱怨。在那份報表裡多一小段：昨天喚醒幾次、執行了幾成、被修正幾次、守衛擋了幾次，就能每天看到這次重構有沒有讓事情變好，也是 D15「一週後評估 ConfirmPolicy」要用的數字。
Stakes if we pick wrong: 不做的話，TurnOutcome 的數據要特地去撈才看得到，D15 一週後評估時要手動算；做了則報表多一段。
Recommendation: A 加進本計畫 because 量測沒人看等於沒量；接進每天已經在跑的報表，成本最低、效果最直接。
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) 加進本計畫 (recommended)
  ✅ 每天起手就看到漏斗變化，重構有沒有效一目了然
  ✅ D15 一週後評估 ConfirmPolicy 時數字已經現成
  ❌ 每日報表多一段，daily_user_needs.py 要多一組測試
B) 延後到 TODOS.md
  ✅ 這波範圍不變
  ✅ 等 TurnOutcome 欄位穩定後再做報表
  ❌ 前一兩週的數據沒人看，問題可能晚發現
C) 不做
  ✅ 報表維持原樣
  ✅ 不多花任何時間
  ❌ 漏斗數據要每次手動撈，D15 評估時要另外算
Net: 每天自動看到漏斗 vs 需要時再手動撈。
Header: 每日漏斗
A) 加進本計畫 (recommended)
步驟 1 一併在 scripts/daily_user_needs.py 加一段讀 records/turn_outcomes.jsonl：喚醒數、執行率、修正率、守衛擋下數（effort S、risk low）。✅ 每天看得到 ✅ D15 數字現成 ❌ 報表多一段要測
B) 延後到 TODOS.md
寫進 TODOS.md，觸發條件「步驟 1 上線滿 3 天」（effort S、risk low）。✅ 範圍不變 ✅ 欄位穩定後再做 ❌ 前期數據沒人看
C) 不做
不加報表（effort S、零實作、risk low）。✅ 報表不變 ✅ 不花時間 ❌ 每次手動撈

## answered decision (E4)
Commitment comparison:
| Commitment | Source/approval or pending | Current | A | B | C |
|---|---|---|---|---|---|
| 同時對談人數 | pending | 1（WakeSession 單一 target） | 最多 2 人各自有窗口（3b 起） | TODOS.md：等 E1 判斷紀錄顯示有需求再做 | 維持 1 人 |
| D5 窗口綁回應對象 | approved | — | 不變 | 不變 | 不變 |

Question: CEO-D6 — E4: 要不要讓兩個人可以同時各自跟 Marvin 對談（都不用喊名字）？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）SCOPE EXPANSION 候選。
ELI10: 現在對談窗同時只給一個人：A 正在跟 Marvin 聊，B 也想問就得喊「馬文」，而且會把 A 的窗口搶走。合併後每個人都有自己的狀態，技術上可以讓兩個人同時各自聊。但 10/17 才發生過「群聊時對談停不下來、朋友每句都被接」的事故，人越多誤接的機會越大，而且目前沒有數據知道多常有兩人同時想問。
Stakes if we pick wrong: 現在就做，群聊誤接可能變兩倍；不做則兩人同時問時還是要互相搶窗口。
Recommendation: B 延後，等 E1 判斷紀錄證明有需求 because 10/17 群聊誤接剛修好，沒有數據前加人數只是加風險；E1 的紀錄一週就能看出「兩人搶窗口」多常發生。
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) 加進本計畫
  ✅ 朋友一起問 Marvin 時不用互相搶窗口，更像真的在客廳聊天
  ✅ 每人一份狀態的架構本來就支援，實作不大
  ❌ 群聊誤接的機會變多，10/17 那類事故可能重演
B) 延後，等 E1 數據 (recommended)
  ✅ 先用 E1 的判斷紀錄量出「兩人搶窗口」的實際次數再決定
  ✅ 不在剛修好的群聊誤接上再加變數
  ❌ 這段時間兩人同時問還是要喊名字、會互搶窗口
C) 不做
  ✅ 維持一次只聽一個人，行為最可預期
  ✅ 誤接風險最低
  ❌ 往「客廳裡自然插話」的理想少走一步
Net: 現在讓多人同時聊 vs 先量需求再說。
Header: 多人對談
A) 加進本計畫
3b 起最多 2 人各自有對談窗，每人獨立計時；D5 綁對象規則不變（effort M、risk medium）。✅ 不用搶窗口 ✅ 架構已支援 ❌ 誤接機會變多
B) 延後，等 E1 數據 (recommended)
寫進 TODOS.md：觸發條件「E1 紀錄一週內『窗口被他人搶走』≥5 次」（effort S、risk low）。✅ 用數據決定 ✅ 不加風險 ❌ 暫時仍要搶窗口
C) 不做
維持單一對談對象（effort S、零實作、risk low）。✅ 行為可預期 ✅ 誤接最低 ❌ 離理想少一步

## answered decision (E5)
Commitment comparison:
| Commitment | Source/approval or pending | Current | A | B | C |
|---|---|---|---|---|---|
| 撤銷上一個動作 | pending | 無 | 加進步驟 5（點歌移出佇列、音量回復、取消進行中的查詢） | TODOS.md：等 E3 每日報表顯示修正需求再做 | 不做 |
| D3 修正接 last_intent | approved | — | 不變 | 不變 | 不變 |

Question: CEO-D7 — E5: 要不要讓「剛剛那個不對／取消」直接撤回 Marvin 上一個動作？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）SCOPE EXPANSION 候選。
ELI10: D3 已經讓「不是，是 Y」可以改掉上一輪的內容重做。撤銷是另一半：只說「取消」或「剛剛那個不對」，就把 Marvin 剛做的事撤回，例如把剛點的歌移出佇列、把音量調回去、停止還在跑的查詢。但兩個月的對話裡，這類說法只出現 1 次（10/15「馬文取消查詢」）。
Stakes if we pick wrong: 現在做，可能是一條幾乎沒人走的路；不做的話，想撤回時要自己喊下一首或調音量。
Recommendation: B 延後，等 E3 報表顯示修正需求 because 兩個月只有 1 次撤回說法，證據太薄；E3 每天的修正數字出來後再決定。
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) 加進步驟 5
  ✅ 說一句「取消」就撤回，不用記得該喊哪個指令
  ✅ 跟 D3 的修正共用 last_intent，實作順手
  ❌ 兩個月只有 1 次需求，很可能做了沒人用
B) 延後，等 E3 數據 (recommended)
  ✅ 用每日漏斗報表的修正數字判斷撤回到底常不常見
  ✅ 步驟 5 範圍維持小，先把修正做穩
  ❌ 這段時間想撤回仍要自己下對應指令
C) 不做
  ✅ 不多一個會誤觸的寫入動作（「算了」在閒聊裡很常見）
  ✅ 不花任何時間
  ❌ 修正機制只有「改」沒有「撤」，少了一半
Net: 現在補上撤回 vs 等數據證明有需求。
Header: 撤銷動作
A) 加進步驟 5
「剛剛那個不對／取消」→ 撤回上一個可撤回動作：點歌移出佇列、音量回復、取消進行中查詢（effort M、risk medium）。✅ 一句話撤回 ✅ 共用 last_intent ❌ 需求證據薄
B) 延後，等 E3 數據 (recommended)
寫進 TODOS.md：觸發條件「E3 報表兩週內修正 ≥10 次且含撤回語意」（effort S、risk low）。✅ 數據決定 ✅ 步驟 5 範圍小 ❌ 暫時要自己下指令
C) 不做
不做撤回（effort S、零實作、risk low）。✅ 不多誤觸 ✅ 不花時間 ❌ 修正少一半

## answered decision (S1)
Commitment comparison:
| Commitment | Source/approval or pending | Current | A | B | C |
|---|---|---|---|---|---|
| gate decision 存放 | CEO-D3 approved（步驟 1 預留 TurnOutcome 欄位） | 未實作 | 獨立 records/turn_gate_decisions.jsonl，每次判斷一筆（含 turn_key、track），統計以 turn_key 去重 | TurnOutcome 內欄位（照 CEO-D3） | 同 A 的獨立檔 |
| 開始寫入時間 | CEO-D3 | 3a | 3a | 3a（步驟 1 先留空欄位） | 步驟 1 先寫現有 `_apply_wake_guards` 擋下結果，3a 起由 TurnGate 寫完整理由 |
| TurnOutcome 每 turn key 一筆 | OV-2 | — | 不變 | 需在一筆內塞多次判斷 | 不變 |

Question: CEO-D8 — S1: TurnGate 的判斷紀錄要放哪、從哪一步開始寫？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）spec review 發現。
ELI10: 你在 E1 同意記下每次「接／不接」的判斷理由，原本寫成「步驟 1 先在每輪紀錄裡留欄位」。審查發現兩個問題：一句話可能被判斷好幾次（Track A/B），但每輪紀錄規定只寫一筆，塞不下；而且步驟 1 還沒有 TurnGate，留的欄位永遠是空的。改成獨立一個檔、每次判斷一筆比較乾淨。另一個做法是步驟 1 就先記現有守衛擋下的結果，讓每日報表的「守衛擋下數」提早有數字。
Stakes if we pick wrong: 照原案會留一個沒值的欄位，而且多次判斷記不下；選 C 則步驟 1 要多碰一點舊的守衛程式。
Recommendation: C 步驟 1 先記現有守衛結果 because 獨立檔解決一筆塞不下的問題，又讓 E3 每日報表的守衛擋下數從第一天就有，不用等到 3a。
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) 獨立檔、3a 起寫
  ✅ 每次判斷一筆，Track A/B 多次判斷都記得下，跟每輪一筆的規則不衝突
  ✅ 步驟 1 範圍不變，不碰舊守衛程式
  ❌ 守衛擋下數要到 3a 才有，E3 報表前兩週缺這一格
B) 照 CEO-D3：TurnOutcome 預留欄位
  ✅ 紀錄集中在一個檔，查一輪時不用跨檔
  ✅ 不用再改已核准的文字
  ❌ 步驟 1 到 3a 之間欄位全是空的，多次判斷又塞不進一筆
C) 獨立檔、步驟 1 先記現有守衛結果 (recommended)
  ✅ E3 報表的守衛擋下數從步驟 1 第一天就有數字
  ✅ 3a 換 TurnGate 前後有同一份紀錄可以對照，守衛搬錯一眼看得出來
  ❌ 步驟 1 要在舊的守衛程式加一行寫紀錄，碰到安全核心附近
Net: 範圍最小 vs 不改已核准文字 vs 提早有數字又能前後對照。
Header: 判斷紀錄放哪
A) 獨立檔、3a 起寫
3a 起 TurnGate 每次判斷寫一筆到 records/turn_gate_decisions.jsonl（含 turn_key、track、理由代碼），步驟 1 不預留（effort S、risk low）。✅ 多次判斷記得下 ✅ 步驟 1 不變 ❌ 守衛數晚兩週
B) 照 CEO-D3：TurnOutcome 預留欄位
步驟 1 在 TurnOutcome 留 gate decision 欄位，3a 起寫入（effort S、risk low）。✅ 紀錄集中 ✅ 不改核准文字 ❌ 欄位空、塞不下多次判斷
C) 獨立檔、步驟 1 先記現有守衛結果 (recommended)
步驟 1 在 `_apply_wake_guards` 擋下時寫一筆到 records/turn_gate_decisions.jsonl（守衛名稱當理由代碼），3a 起改由 TurnGate 寫完整理由（effort S、risk medium）。✅ 守衛數第一天就有 ✅ 換 TurnGate 前後可對照 ❌ 步驟 1 碰到守衛程式

## answered decision (S2)
Commitment comparison:
| Commitment | Source/approval or pending | Current | A | B |
|---|---|---|---|---|
| 顯示的窗口種類 | CEO-D4（未限定） | — | 對談窗（WakeSession 類）＋主動話題房間窗 | 6 種窗口全部 |
| 關窗編輯同一則、失敗吞掉記 log、WINDOWS=0 時不顯示 | spec review 補註 | — | 同 | 同 |

Question: CEO-D9 — S2: 「🎧 聽 X 中」要對哪些窗口顯示？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）spec review 發現。
ELI10: E2 同意在文字頻道顯示 Marvin 正在聽誰。但窗口有 6 種，有些只開 8 秒（Marvin 問完問題）、12 秒（反問哪一首）、或 PA 的是／否。全部都顯示的話，文字頻道會一直跳訊息。只顯示比較長、比較重要的對談窗，以及主動話題問全房間的窗口，比較不吵。
Stakes if we pick wrong: 全顯示會刷頻、大家反而不看；只顯示兩種則短窗口開著時大家看不到。
Recommendation: A 只顯示對談窗與房間窗 because 對談窗 16 天接住 80 次是主要窗口，短窗口幾秒就關，顯示了也來不及看。
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) 只顯示對談窗與房間窗 (recommended)
  ✅ 文字頻道不會被幾秒就關的短窗口洗版
  ✅ 涵蓋最常用的對談窗（16 天接住 80 次）和需要大家知道的房間窗
  ❌ 反問哪一首、PA 是／否這類短窗口開著時沒有顯示
B) 所有窗口都顯示
  ✅ 任何時候 Marvin 在聽誰都看得到，規則最單純
  ✅ 方便除錯窗口行為
  ❌ 8 秒、12 秒的窗口一直發訊息又改訊息，容易刷頻
Net: 安靜只顯示重要的 vs 全部看得到但吵。
Header: 顯示哪些窗口
A) 只顯示對談窗與房間窗 (recommended)
只有 WakeSession 類對談窗與 D5 主動話題房間窗開窗時顯示「🎧 聽 X 中」（房間窗顯示「🎧 聽大家回答中」），關窗編輯同一則（effort S、risk low）。✅ 不刷頻 ✅ 涵蓋主要窗口 ❌ 短窗口不顯示
B) 所有窗口都顯示
6 種窗口開窗都顯示、關窗都編輯（effort S、risk low）。✅ 全看得到 ✅ 好除錯 ❌ 容易刷頻

## answered decision (S3)
Commitment comparison:
| Commitment | Source/approval or pending | Current | A | B | C |
|---|---|---|---|---|---|
| TurnOutcome 欄位 | D12 白名單 | 無 bid | 加 winner_agent、winner_bid（非原文） | 加 winner_agent、winner_bid | 不加 |
| D15/T2 觸發條件 | T2 approved「步驟 1 上線滿 7 天」 | 步驟 1 滿 7 天 | 改「步驟 5 上線滿 7 天」（修正率才有值） | 不變；修正率改用 stt_history 人工標註 | 不變（屆時兩個數字都量不到） |

Question: CEO-D10 — S3: ConfirmPolicy 一週後的評估，要怎麼讓它真的量得到數字？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）spec review 發現。
ELI10: D15 把 ConfirmPolicy 延後，說「步驟 1 上線滿 7 天」再看兩個數字：判錯後被修正的比率、各 agent 的出價分數分佈。審查發現兩個都量不到：每輪紀錄沒記出價分數；「被修正」要到步驟 5 做完修正機制才有值。補上出價分數欄位很便宜（不是原文，不違反 D12）；修正率則要嘛等步驟 5，要嘛用人工對 stt_history 標註。
Stakes if we pick wrong: 維持原樣的話，一週後打開報表兩個數字都是空的，延後就變成無限期延後。
Recommendation: A 加欄位、觸發改步驟 5 後 because 兩個數字都會自動有值，不用人工標註；代價只是評估晚幾天。
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) 加欄位、觸發改成步驟 5 上線滿 7 天 (recommended)
  ✅ 屆時出價分佈和修正率都有自動數據，不用人工標註
  ✅ 欄位只是 agent 名稱和分數，不碰 D12 不存原文的規則
  ❌ ConfirmPolicy 的評估要等到步驟 5 之後，比原本晚
B) 加欄位、觸發不變、修正率人工標註
  ✅ 步驟 1 滿 7 天就能評估，比較早
  ✅ 出價分佈有自動數據
  ❌ 修正率要人工對 stt_history 標註，費時且樣本有限
C) 維持原樣
  ✅ 不改任何已核准的文字
  ✅ 不多任何欄位
  ❌ 一週後兩個數字都量不到，評估無法進行
Net: 晚一點但全自動 vs 早一點但要人工 vs 量不到。
Header: D15 怎麼量
A) 加欄位、觸發改成步驟 5 上線滿 7 天 (recommended)
TurnOutcome 加 winner_agent、winner_bid；TODOS.md 的 ConfirmPolicy 回頭評估觸發條件改為「步驟 5 上線滿 7 天」（effort S、risk low）。✅ 全自動 ✅ 不違反 D12 ❌ 評估較晚
B) 加欄位、觸發不變、修正率人工標註
加同樣兩個欄位；觸發維持步驟 1 滿 7 天，修正率改用 stt_history 人工標註樣本（effort S、risk low）。✅ 較早 ✅ 出價自動 ❌ 修正率靠人工
C) 維持原樣
不加欄位、不改觸發（effort S、零實作、risk low）。✅ 不改文字 ✅ 不加欄位 ❌ 量不到

## answered decision (S4)
Commitment comparison:
| Commitment | Source/approval or pending | Current | A | B | C |
|---|---|---|---|---|---|
| PR#139（對談窗跳 rescue、gap classifier 預設關）時機 | 未定；記憶 project_fast_followup_routing 標 ⬜merge | 開著未合併 | 步驟 1 之前合併，3a 情境表納入其行為 | 凍結到本計畫完成後再議 | 關掉，本計畫完成後在 TurnGate 上重做 |
| 一天 1–2 個 PR 節奏 | feedback_batch_prs_after_suspension | — | PR#139 佔一個名額 | 不佔 | 不佔 |

Question: CEO-D11 — S4: 還沒合併的 PR#139（對談窗加速）要先合，還是等這個計畫做完？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）spec review 發現。
ELI10: PR#139 是 10/20 做的對談加速：對談窗內跳過 Audio Rescue、gap classifier 預設關。它已經部署在跑，但還沒合進 main。它改的正好是步驟 3b（窗口）和步驟 4（bus）要動的地方。如果不先決定順序，兩邊會互相衝突。先合的話，本計畫直接以它為基礎、情境表也把它的行為算進去；晚合的話，它要重新對齊本計畫。
Stakes if we pick wrong: 不決定的話，3b 和步驟 4 寫完時跟 PR#139 衝突，要花時間解；凍結或重做則已經在跑的加速成果要等很久才進 main。
Recommendation: A 步驟 1 之前合併 because PR#139 已經在 prod 跑、行為已被驗證，先合進 main 讓本計畫以實際運作的程式為基礎，衝突最少。
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) 步驟 1 之前合併 (recommended)
  ✅ 本計畫以 prod 實際在跑的程式為基礎，不會寫完才發現衝突
  ✅ 對談加速的成果早點進 main，不會一直掛在分支上
  ❌ 佔掉一個 PR 名額，步驟 1 晚一天開始
B) 凍結到本計畫完成後
  ✅ 本計畫的 PR 節奏不受影響
  ✅ 計畫完成後再看 PR#139 還需不需要
  ❌ prod 跑的程式跟 main 長期不一致，3b、步驟 4 照 main 寫會跟 prod 行為不同
C) 關掉 PR#139，之後在 TurnGate 上重做
  ✅ 不用處理衝突，加速邏輯直接放進新架構
  ✅ 程式結構最乾淨
  ❌ 已經驗證過的加速要重做一次，而且重做前 prod 和 main 不一致
Net: 先合省衝突 vs 不影響節奏 vs 重做最乾淨。
Header: PR139 順序
A) 步驟 1 之前合併 (recommended)
先合 PR#139（含 #140 疊加的變更）進 main，本計畫各 PR 以合併後的 main 為基礎，3a 情境表納入「對談窗內跳過 rescue」行為（effort S、risk low）。✅ 衝突最少 ✅ 成果進 main ❌ 佔一個 PR 名額
B) 凍結到本計畫完成後
PR#139 維持開著不合，本計畫以現在的 main 為基礎（effort S、risk medium）。✅ 節奏不受影響 ✅ 之後再評估 ❌ prod 與 main 不一致
C) 關掉 PR#139，之後在 TurnGate 上重做
關掉 PR#139，本計畫完成後把加速邏輯放進 TurnGate 重做（effort M、risk medium）。✅ 無衝突 ✅ 結構乾淨 ❌ 已驗證的成果要重做

## answered decision (S5)
Commitment comparison:
| Commitment | Source/approval or pending | Current | A | B |
|---|---|---|---|---|
| last_intent 存活時間 | §7 未決（建議沿用 WakeSession idle） | — | 120s（＝WakeSession idle） | 30s |
| 誰能修正 | 未定 | — | 只認同一個 speaker | 只認同一個 speaker |
| 要不要喊馬文 | 未定 | — | 對談窗內免喊，窗外要喊 | 一律要喊 |
| WINDOWS=0 時 | D14 | — | 修正關閉，走一般路徑 | 同 |

Question: CEO-D12 — S5: 「不是，是 Y」的修正，在什麼條件下才算數？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）spec review 發現。
ELI10: D3 決定 Marvin 要記住上一個意圖，讓使用者說「不是，是周杰倫那首」就能改。但幾個規則還沒定：上一個意圖記多久、別人能不能幫忙改、要不要喊馬文。文件原本建議跟對談窗一樣記 120 秒，對談窗內不用喊名字；另一個做法是只記 30 秒、而且一律要喊名字，比較不會誤觸。不管哪一種都只認同一個人。
Stakes if we pick wrong: 太寬鬆的話，閒聊裡的「不是啦」可能被當成修正、把歌改掉；太嚴格的話，使用者想改時常常已經過期或忘了喊名字。
Recommendation: A 跟對談窗一致 because 修正大多發生在剛回完話的對談窗裡，跟窗口同一套規則使用者最好預期；窗外要喊馬文就擋掉了閒聊誤觸。
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) 跟對談窗一致：120 秒、窗內免喊 (recommended)
  ✅ 跟對談窗同一套規則，使用者不用另外記修正的限制
  ✅ 窗外一律要喊馬文，閒聊裡的「不是啦」不會被誤當修正
  ❌ 窗內 120 秒都有效，對談中隨口一句「不是」可能被當修正
B) 30 秒、一律要喊馬文
  ✅ 最不會誤觸，只有明確叫馬文才會改
  ✅ 規則最簡單，不依賴窗口狀態
  ❌ 30 秒很容易過期，而且要喊名字比較不像自然對話
Net: 跟窗口一致好預期 vs 最嚴格不誤觸。
Header: 修正規則
A) 跟對談窗一致：120 秒、窗內免喊 (recommended)
last_intent 存活 120 秒（＝WakeSession idle）；只認同一 speaker；對談窗內免喊、窗外要喊馬文；WINDOWS=0 時修正關閉（effort S、risk medium）。✅ 好預期 ✅ 窗外不誤觸 ❌ 窗內可能誤當修正
B) 30 秒、一律要喊馬文
last_intent 存活 30 秒；只認同一 speaker；一律要喊馬文；WINDOWS=0 時修正關閉（effort S、risk low）。✅ 最不誤觸 ✅ 規則簡單 ❌ 容易過期、不自然

## answered decision (S6)
Commitment comparison:
| Commitment | Source/approval or pending | Current | A | B |
|---|---|---|---|---|
| 3a PR 數 | D13 approved（3a 一個 PR） | 1 | 2（3a-1、3a-2），總 PR 7→8（另加 PR#139 先合） | 1 |
| 3a 內容 | D11/D13/D18/CEO-D8 | — | 3a-1：精確測試清單＋三介面＋薄轉接＋`_apply_wake_guards` 回傳 (blocked, reason_code)；3a-2：測試改寫＋情境表＋Echo Guard 50 筆人工分類 | 同內容一個 PR |
| 估時 | spec review 可行性 3 | CC ~2h | 3a-1 CC ~1h；3a-2 CC ~3h＋人工分類 ~1h（人力） | CC ~4h＋人工 ~1h |

Question: CEO-D13 — S6: 步驟 3a 工作量太大，要不要再拆成兩個 PR？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）spec review 發現。
ELI10: 3a 要做的事很多：先列出要改寫的 266–386 個測試、建三個新介面加一層轉接、把測試改寫過去、做真實情境表、還要人工分類 50 筆 Echo Guard 樣本。原本估「2 小時」不實際，人工分類也不能交給 Claude。拆成兩段：第一段先把介面和轉接搭好（讓舊守衛能回報是哪一道擋的），第二段再改寫測試和做情境表。
Stakes if we pick wrong: 不拆的話，3a 這個 PR 會非常大、審查困難，出問題很難定位；拆的話多一個 PR、多一天。
Recommendation: A 拆兩段 because 介面和測試改寫分開審，每一段都看得懂；人工分類也能另外排時間，不卡住介面那段。
Completeness: A=10/10, B=8/10
Pros / cons:
A) 拆成 3a-1、3a-2 (recommended)
  ✅ 每個 PR 小到審得完，介面設計和測試改寫分開檢查
  ✅ 人工分類 50 筆可以另排時間，不卡住介面那段
  ❌ 總 PR 數 7→8，照一天 1–2 個多一天
B) 維持一個 PR、修正估時
  ✅ PR 數不變，節奏不受影響
  ✅ 介面跟測試在同一個 PR，一次看完整
  ❌ PR 非常大（數百個測試改寫加新介面），審查與定位問題都困難
Net: 多一個 PR 換可審查性 vs 少一個 PR 但巨大。
Header: 3a 拆分
A) 拆成 3a-1、3a-2 (recommended)
3a-1：精確測試清單、TurnGate／ConversationState／RoomState 介面、薄轉接、`_apply_wake_guards` 回傳 (blocked, reason_code)（允許的最小舊碼改動）；3a-2：測試改寫、情境表、Echo Guard 人工分類（人力 ~1h）（effort M、risk medium）。✅ 可審查 ✅ 人工另排 ❌ 多一個 PR
B) 維持一個 PR、修正估時
3a 維持一個 PR，估時改為 CC ~4h＋人工 ~1h（effort M、risk medium）。✅ PR 數不變 ✅ 一次看完整 ❌ PR 巨大

## 執行規格定稿（CEO review 後）

這一節取代上方「審查核准版計畫」的 PR 順序、Failure modes、Worktree parallelization、Implementation Tasks，以及 §6 原稿驗收標準。

### 架構
```
STT（engine 在 loop 上 await；Track A/B/WakeStream/wake_check 以 turn_key=speaker+first_audio 去重）
 ├─ sink 執行緒 speech-start ──bridge(call_soon_threadsafe)──┐  （D7：只有此入口需要 bridge）
 ▼                                                          ▼
TurnGate ─ RoomState（Response Lock／Storm／Global 2s／D5 主動話題房間窗）
        ─ ConversationState × speaker（WakeSession 類對談窗／D5 綁對象問句窗／反問接續／dialogue 等問句／PA yes-no／barge_in_pending／last_intent）
        ─ 7 道守衛（D6）；守衛例外 fail-closed、窗口例外 fail-open＝當作沒窗口（D8）
        ─ 每次判斷寫 records/turn_gate_decisions.jsonl（CEO-D8）
 ▼
IntentBus（IntentContext.expecting；NemoClaw 併入（D10）；Marmo/PA/狀態/模仿/music drop 收成 agent（D1）；base.py ValueError（D16））
 ▼ handler
 ▼ 修正：last_intent 120s、只認同一 speaker、窗內免喊／窗外要喊（CEO-D12）
 ▼ TurnOutcome（每 turn_key 一筆；欄位：turn_id、時間、speaker、discovered_by、intent/slot 名、winner_agent、winner_bid、executed、corrected、satisfaction_signal；不存原文（D12、CEO-D10））
（ConfirmPolicy 延後，觸發「步驟 5 上線滿 7 天」（D15、CEO-D10））
```

### 資料格式
**records/turn_gate_decisions.jsonl**（CEO-D8）：每次判斷一筆。欄位：`ts`、`turn_key`、`track`（A/B/stream/wake_check/text）、`speaker`、`decision`（accept/block/ignore）、`reason`、`window`（無則 null）、`iba_confidence`（`voice_controller.py:1159` 的 IBA total，無則 null）。不存原文。統計以 turn_key 去重取最後一筆。量約每天 1–2 千筆；保留 30 天（CEO-D15）。
理由代碼列舉：`guard_double_wake`、`guard_response_lock`、`guard_storm`、`guard_echo`、`guard_global`、`guard_per_speaker`、`guard_exception`、`window_session`、`window_question`、`window_followup`、`window_dialogue`、`window_pa`、`window_room`、`window_preempted_by_other`、`wake_word`、`none`。
步驟 1 只寫得出守衛類代碼（`_apply_wake_guards` 擋下時以守衛名稱寫入）；3a-1 起 `_apply_wake_guards` 回傳 `(blocked, reason_code)`，3b/3c 起由 TurnGate 寫完整代碼。

### 行為規則
- **單一對談對象（E4 延後前提）**：同時只有一個 WakeSession 類 target；新喚醒搶走舊窗口時寫 `window_preempted_by_other`（E4 觸發計數）。
- **E2 顯示**（CEO-D9）：只有 WakeSession 類對談窗與 D5 房間窗顯示；文案「🎧 聽 {speaker} 中」／房間窗「🎧 聽大家回答中」；關窗由 loop 上計時器觸發，編輯同一則為「已結束」；需保存訊息物件；發送／編輯失敗吞掉並記 log；`MARVIN_TURN_GATE_WINDOWS=0` 時不顯示。
- **kill flag 組合**（D14）：四種組合都支援並各有測試；`WINDOWS=0` 時修正（步驟 5）關閉、E2 不顯示；`GUARDS=0` 時守衛走 3a 轉接的舊路徑、gate decision 仍寫（理由代碼來自舊守衛回傳）。
- **守衛例外告警**（D8 補強）：守衛例外寫 `guard_exception`；同一守衛連續 3 次例外時 `logger.error`（經既有 ErrorDispatcher 寫 incident 並 DM owner），並有測試。
- **E3 分階段**（CEO-D5）：步驟 1 報表出喚醒數、執行率、守衛擋下數（CEO-D8 使步驟 1 即有守衛數）；修正率從步驟 5 起出現；缺資料欄位顯示「尚無資料」，各有測試。

### PR 順序（一天 1–2 個）
0. **合併 PR#139（含 #140）**（CEO-D11）。
1. **TurnOutcome＋gate decision（守衛類）＋E3 報表第一階段**。驗收：TurnOutcome 每 turn_key 一筆（含 Track A/B、重投路徑、WakeShortcut 提前 return）；不含原文欄位的斷言；寫檔失敗不拋；守衛擋下寫 gate decision；daily_user_needs.py 漏斗段三個數字＋「尚無資料」測試；上線 24h 覆蓋 ≥95% 喚醒。
2. **刪除零流量路徑**。驗收：每條附 logger 等級確認＋流量證據；使用者拍板清單；全套 pytest 綠。
3a-1. **介面＋薄轉接**（CEO-D13）。驗收：精確測試退役清單（含 retirement card 範本）；TurnGate／ConversationState／RoomState 介面；薄轉接接舊實作；`_apply_wake_guards` 回傳 reason_code（唯一允許的舊碼行為面改動，既有測試全綠）；`IntentContext.expecting`。
3a-2. **測試改寫＋情境表**。驗收：窗口/守衛相關既有測試改寫打新介面、對舊實作全綠；真實情境表每窗口/守衛 ≥5 條（含 PR#139「對談窗跳 rescue」、Echo Guard 50 筆三分類，人工 ~1h）對舊實作全綠。
3b. **換 6 套窗口**。驗收：情境表＋改寫測試全綠；D5 addressee 傳進 play_tts；dialogue state 改寫保留攔截順序；不再合成重投；單一 target 與 `window_preempted_by_other`；E2 顯示規則與失敗測試；`WINDOWS=0` 退回測試。
3c. **換 7 道守衛**。驗收：守衛 fail-closed／窗口 fail-open 失敗測試；`guard_exception` 連續 3 次告警測試；pending.cancel() 經 bridge 的跨執行緒測試；`GUARDS=0` 退回測試；四種 flag 組合測試。
4. **bus 收編**。驗收：「單講龍蝦」情境、regex 聯集表、ValueError 紅測；門檻 0.80→0.65 單獨 commit；winner_agent／winner_bid 寫入 TurnOutcome。
5. **修正接 last_intent**（CEO-D12）。驗收：120s 邊界、同 speaker、窗內免喊／窗外要喊、`WINDOWS=0` 關閉的測試；E3 報表出現修正率。
6. **清理**（3b/3c 穩定一週後）：刪舊路徑、兩個 kill flag，並在此時驗收「窗口相關 self 屬性從 voice_controller.py 移除」（原 §6 條件，與 D14 不衝突）。

### Failure modes
| 路徑 | 實際失敗 | 測試／處理 | 使用者看到 |
|---|---|---|---|
| sink 執行緒入口 | 非 loop 建 task 失效 | D7 bridge＋跨執行緒測試 | （已修 cc617b1） |
| 守衛例外 | 每次都例外 → 整晚喚不醒 | fail-closed＋連續 3 次 ErrorDispatcher 告警 | 漏聽；owner 收到 DM |
| 窗口例外 | 例外 | fail-open（當作沒窗口） | 要喊馬文 |
| Track A/B 重複 | 一次性 expectation 被重複消費 | turn_key 去重＋情境 | （修好前反問被吃） |
| 3b/3c 搬錯 | 自我觸發／喚不醒 | 3a 舊實作先綠＋情境表＋kill flag | 改 env 重啟退回 |
| E2 發送／編輯失敗 | Discord 限流 | 吞掉記 log＋測試 | 少一則提示，不影響對話 |
| gate decision／TurnOutcome 寫檔 | IO 例外 | 吞例外＋測試 | 無 |
| 修正誤觸 | 窗內「不是」被當修正 | 只認同 speaker＋120s＋情境 | 偶爾改錯，可再說一次 |
Critical gaps：0。

### Worktree parallelization
| Step | Modules | Depends on |
|---|---|---|
| 0 PR#139 | cogs/voice_controller*、intent_bus | — |
| 1 | pipeline/、cogs/voice_controller*、scripts/ | 0 |
| 2 | cogs/voice_controller*、wake_detector | 1 |
| 3a-1 | wake/、cogs/voice_controller*、intent_bus | 2 |
| 3a-2 | tests/ | 3a-1 |
| 3b | wake/、cogs/voice_controller*、playback | 3a-2 |
| 3c | wake/、cogs/voice_controller* | 3b |
| 4 | intent_agents/、intent_bus、cogs/voice_controller* | 3a-1 |
| 5 | wake/、intent_bus | 3b、4 |
依序實作，無實質平行化機會（步驟 4 與 3b/3c 同改 cogs/voice_controller*，CEO Outside Voice 第 12 條）。人工 Echo Guard 分類可在 3a-1 期間由使用者先做。

### Implementation Tasks
- [ ] **T0 (P1, human: ~1h / CC: ~10min)** — 合併 PR#139／#140（CEO-D11）。Verify：CI 綠、main 與 prod 一致。
- [ ] **T1 (P1, human: ~1 天 / CC: ~45min)** — TurnOutcome＋守衛類 gate decision＋E3 第一階段。Files：新 turn_outcome 模組、turn_gate_decisions 寫入、cogs/voice_controller.py、scripts/daily_user_needs.py。Verify：見 PR 1 驗收。
- [ ] **T2 (P1, human: ~半天 / CC: ~20min)** — 零流量路徑清單與刪除。
- [ ] **T3a1 (P1, human: ~1 天 / CC: ~1h)** — 介面＋薄轉接＋reason_code。
- [ ] **T3a2 (P1, human: ~2 天 / CC: ~3h＋人工 ~1h)** — 測試改寫＋情境表＋Echo Guard 分類。
- [ ] **T4 (P1, human: ~2 天 / CC: ~1.5h)** — 3b 窗口＋E2＋單一 target＋WINDOWS flag。
- [ ] **T5 (P1, human: ~2 天 / CC: ~1.5h)** — 3c 守衛＋guard_exception 告警＋GUARDS flag＋四組合測試。
- [ ] **T6 (P2, human: ~2 天 / CC: ~1h)** — bus 收編＋winner_agent/bid。
- [ ] **T7 (P2, human: ~1 天 / CC: ~30min)** — 修正接 last_intent（CEO-D12）＋E3 修正率。
- [ ] **T8 (P3, human: ~1h / CC: ~10min)** — 清理舊路徑與 flag，驗收 self 屬性移除。

### 定稿補充（spec review 第 2 輪；已核准範圍的必要實作與事實更正，不另議）
1. **理由代碼完整列舉**：在上方列舉外補 `window_deferred`（步驟 2 若刪 deferred wake 就一併移除）、`bypass_strong_voice`、`bypass_intent`、`guard_followup_override`。列舉隨步驟 2 的刪除結果更新。
2. **gate decision 三階段分工**：步驟 1～3a-2 由薄轉接層依舊守衛回傳的 reason_code 寫；3b 起 TurnGate 寫窗口類代碼（守衛類仍由轉接層寫）；3c 起 TurnGate 寫全部代碼。
3. **搶窗事件**：新喚醒搶走舊窗口時另寫一筆 `decision=preempt`，欄位加 `preempted_speaker`、`reason=window_preempted_by_other`；此類紀錄不參與 turn_key 去重（E4 觸發用這類筆數計）。
4. **`_apply_wake_guards` 回傳**（更正）：現行回傳 `(is_fast, is_echo)`（voice_controller.py:1191）。3a-1 改為在尾端加一個元素 `(is_fast, is_echo, reason_code)`，呼叫端多解一個值；既有語意不變，守衛測試只補第三個值的斷言。
5. **守衛副作用唯一擁有者**：記 segment、開 Response Lock、累加風暴計數這些副作用，3a～3b 期間由舊 `_apply_wake_guards` 執行，3c 起改由 TurnGate 執行；任何 flag 組合下只執行一次，補「副作用不重複」測試。
6. **TurnOutcome 欄位表**：
   | 欄位 | 型別／值 | 來源 | 何時有值 |
   |---|---|---|---|
   | turn_id | str（turn_key） | engine | 步驟 1 |
   | ts、speaker | float、str | engine | 步驟 1 |
   | discovered_by | wake_word／wake_shortcut／session／question／followup／dialogue／pa／room／deferred／text | 步驟 1 依現有分支推斷，3b 起 TurnGate 給 | 步驟 1 |
   | intent、slots | agent 名、slot 名列表（不含值） | IntentBus | 步驟 1 |
   | winner_agent、winner_bid | str、float | IntentBus winner | 步驟 4（CEO-D10） |
   | executed | bool：handler 有被呼叫且未例外 | dispatch 結果 | 步驟 1 |
   | corrected | null（步驟 5 前）／bool | 步驟 5 修正機制 | 步驟 5 |
   | satisfaction_signal | null／skip／like／frustration／barge_in | 既有 skip、like、FrustrationAgent、barge-in 紀錄以 turn_id 或時間對齊 | 步驟 1（能對齊者） |
   比率公式：執行率＝executed=true 的 turn 數 ÷ 去重後 turn 數；修正率＝corrected=true ÷ executed=true；守衛擋下數＝turn_gate_decisions 中 decision=block 且 reason 為 guard_* 的去重 turn 數。E3 報表同時讀 turn_outcomes.jsonl 與 turn_gate_decisions.jsonl。
7. **turn_key 邊界**：長句超過 `MAX_AUDIO_CHUNK_DURATION` 被切段而重設 first_audio 時，各段是不同 turn（照實記錄，不合併）；文字輸入（track=text）以 `speaker+收到時間` 當 turn_key。各補一個測試。
8. **consent 與保留**：未同意者在 `handle_stt_result` 開頭即 return（voice_controller.py:1032），不會產生任何 TurnOutcome 或 gate decision，兩檔不需另外處理；兩檔保留 30 天，於 scripts/run_maintenance.py 3am 作業清理（CEO-D15）。
9. **E2 邊界**：窗口被搶時舊訊息編為「🎧 已結束」、新 target 發新訊息；idle 延長時不改訊息（只延後關窗計時器）；bot 重啟時不處理重啟前留下的「聽 X 中」訊息（訊息 id 只存在記憶體，重啟即遺失；不落檔）；`active_text_channel` 為 None 時略過顯示並記 debug log；顯示走 `active_text_channel`（voice_controller_playback.py:609 同一個頻道）；「已結束」文案為「🎧 已結束」。3b 驗收每條各一個測試。
10. **房間窗 TTL**：D5 房間窗沿用現行 `MARVIN_FOLLOWUP_WINDOW_SEC`（預設 8 秒）。它被列為 E2 顯示例外的理由：房間窗是「問全房間」，正是大家不知道能不能直接回答的時刻（CEO-D9 理由補註）。
11. **步驟 2 開工條件**：步驟 1 上線滿 3 天，且 discovered_by 能區分每條候選路徑，才開步驟 2。
12. **棘輪**（tests/test_voice_controller_size_budget.py：STATEMENT_BUDGET、SELF_ATTR_BUDGET、FROZEN_METHODS 只准降）：所有新寫入點放在 mixin 或純函式模組（turn_outcome、turn_gate、conversation_state），voice_controller.py 只放一行呼叫；每個 PR 驗收加「棘輪測試通過、預算數字不調高」。
13. **補回原 §6 驗收**：3b、3c 各自上線一週後，WakeSession 類接住數與 followup 接走數不低於上線前一週（以 turn_gate_decisions 對照）。
14. **守衛例外告警（CEO-D14=A 核准）**：D8 選 fail-closed，若沒有告警，守衛持續例外時會變成「整晚喚不醒且無人知道」的無聲失敗（違反本計畫「無聲失敗＝0」的 Failure modes 要求）；做法沿用既有 ErrorDispatcher，不新增通道。
15. **步驟 0 前提**：PR#140 的 base 是 PR#139 的分支（perf/fast-followup-routing），prod 目前跑 cc617b1（barge-in 分支，基底 d4b4bf9＝本機合併 feat/session-qa-context，含 #140 之後的 7dd73a1 等 commit）。順序：#139 → #140 合進 main → d4b4bf9 之後未進 PR 的 commit 與 feat/session-barge-in 各自 rebase 到新 main 開 PR。T0 驗收改為「#139、#140 已合併；`git merge-base --is-ancestor <#140 merge commit> HEAD` 對之後每個本計畫分支成立；CI（workflow CI）綠」。
16. **人工分類交付物**：Echo Guard 50 筆三分類存 `records/echo_guard_audit_2026-10.jsonl`（每筆 ts、turn_key、類別，不存原文），分類由使用者在 3a-1 期間完成；指令類 >10% 時在 TODOS.md 新增「Echo Guard 誤擋修正」並附比率。
17. **3a-2 估時**：3a-1 產出精確清單後重估；超過 4 小時就依窗口／守衛分批（同一 PR 內分 commit）。

### 定稿補充（spec review 第 3 輪；審查上限已到，以下為作者修正，未經第 4 輪重審）
G1. **晚到訊號**：TurnOutcome 主紀錄在派發當下寫一筆；之後才知道的欄位（corrected、satisfaction_signal、背景 task 例外）另寫 `turn_update` 事件（turn_id、field、value、ts），報表依 turn_id 合併。`executed` 改定義為「同步派發未丟例外」；背景 task 用 done-callback，例外時寫 update `executed=false`。
G2. **哪些輪寫 TurnOutcome**：只有通過發現階段（喊喚醒詞、WakeShortcut、或被窗口接住）的輪才寫；被守衛擋下的只在 turn_gate_decisions。喚醒數＝discovered_by ∈ {wake_word, wake_shortcut} 的去重 turn 數。
G3. **步驟 5 動工前必補**：修正規則表（辨識方式、可修正的意圖、executed=false 時 last_intent 不更新、「對談窗內」只算 WakeSession 類、last_intent 在記憶體保存 slot 值但不落檔）與真實 log 的 BDD 情境；並寫明如何以上一輪意圖組出 ctx 呼叫 `_resolve_and_redispatch`（intent_bus.py:585）。在補齊前不得開工步驟 5。
G4. **route 欄位**：TurnOutcome 加 `route`（例：`bus:music`、`legacy:nemoclaw_fasttrack`、`legacy:status_keywords`、`legacy:imitate`、`legacy:music_drop`），步驟 2 以 route 計每條候選路徑流量。
G5. **補充第 13 條對照基準**改用 turn_outcomes 的 discovered_by（session、followup），步驟 1 起就有值。
G6. **gate decision 去重規則統一**：同一 turn_key 多筆時優先順序 accept > block > ignore（任一筆 accept 即算接受）；preempt 事件不參與去重。守衛擋下數＝去重後判定為 block 且 reason 為 guard_* 的 turn 數。
G7. **步驟 1 寫入者**：步驟 1 起 `_apply_wake_guards` 就回傳 `(is_fast, is_echo, reason_code)`（自 3a-1 提前，理由見 G9），由呼叫端後一行的 mixin 呼叫寫 gate decision；3a-1～3a-2 由薄轉接層寫；3b/3c 依 G8。
G8. **flag 2×2 負責表**：
   | WINDOWS / GUARDS | 副作用（segment、Response Lock、風暴計數） | 守衛類 gate decision | 窗口類 gate decision ＆ discovered_by |
   |---|---|---|---|
   | 1 / 1 | TurnGate | TurnGate | TurnGate |
   | 1 / 0 | 舊 `_apply_wake_guards` | 轉接層（舊回傳） | TurnGate |
   | 0 / 1 | TurnGate | TurnGate | 轉接層（依舊窗口分支推斷） |
   | 0 / 0 | 舊 `_apply_wake_guards` | 轉接層 | 轉接層 |
   補充第 2、5 條的「3c 起由 TurnGate」皆以此表為準。
G9. **棘輪**：`_apply_wake_guards`（FROZEN 79）與 `handle_stt_result`（FROZEN 187）只准縮。步驟 1 新增的呼叫（gate decision、WakeShortcut 提前 return 的 outcome）必須在同一 PR 內從同函式抽出等量程式到 mixin，使淨 statement 不增；PR 驗收以棘輪測試通過為準。動工前先量兩函式現值。
G10. **步驟 0 擴大**：T0 改為「#139、#140、feat/session-barge-in 與 d4b4bf9 之後未進 PR 的 prod commit 全部合進 main，且 main 包含 cc617b1」，才能開步驟 1。這會佔 2–3 個 PR 名額，T0 估時改 human ~1 天／CC ~30 分。
G11. **PR 計數**：本計畫 PR＝1、2、3a-1、3a-2、3b、3c、4、5、6（清理）共 9 個；另有步驟 0 的既有 PR（#139、#140、barge-in 等）不計入。CEO 摘要的「7→8」指不含清理與步驟 0 的計數，已同步更正。
G12. **步驟 6「穩定一週」**：補充第 13 條達標、期間未切過 kill flag、無 guard_exception 告警。
G13. **步驟 4 範圍**＝步驟 1 route 清單中屬 legacy 的路徑，扣掉步驟 2 已刪除者。

## answered decision (S7)
Commitment comparison:
| Commitment | Source/approval or pending | Current | A | B |
|---|---|---|---|---|
| 守衛例外時的通知 | D8 只核准 fail-closed＋失敗測試 | 無 | 同一守衛連續 3 次例外 → logger.error 經既有 ErrorDispatcher 寫 incident＋DM owner，並有測試 | 只寫 guard_exception 代碼、不告警 |
| guard_exception 代碼 | CEO-D8 列舉 | — | 寫 | 寫 |

Question: CEO-D14 — S7: 守衛程式連續出錯時，要不要主動通知你？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）spec review 第 3 輪發現。
ELI10: D8 決定守衛出錯時「寧可漏聽」：擋下這句、不讓 Marvin 自己觸發自己。但如果某道守衛每次都出錯，結果就是整晚喚不醒，而且沒人知道。專案已經有 ErrorDispatcher：錯誤會寫進 incident 並私訊你。這裡要決定的是：同一道守衛連續出錯 3 次時，要不要走這條通道通知你。
Stakes if we pick wrong: 不通知的話，守衛壞掉時要等朋友抱怨「Marvin 都不理人」才知道；通知的話，偶發的連續錯誤也會私訊你。
Recommendation: A 連續 3 次就通知 because 沿用已有的通道、不新增任何東西，就能把「整晚喚不醒」從無聲失敗變成會被發現的失敗。
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) 連續 3 次就通知 (recommended)
  ✅ 守衛壞掉時當下就收到私訊，不用等朋友抱怨
  ✅ 走既有 ErrorDispatcher，不新增通道或設定
  ❌ 偶發的連續 3 次錯誤也會私訊你，可能有雜訊
B) 只記代碼、不通知
  ✅ 不會有任何新的私訊打擾
  ✅ 事後仍可從 turn_gate_decisions 查到 guard_exception
  ❌ 守衛持續壞掉時沒人即時知道，可能整晚喚不醒
Net: 壞了馬上知道 vs 不被打擾。
Header: 守衛告警
A) 連續 3 次就通知 (recommended)
同一守衛連續 3 次例外 → logger.error 經既有 ErrorDispatcher 寫 incident 並 DM owner；3c 驗收含此測試（effort S、risk low）。✅ 即時知道 ✅ 沿用通道 ❌ 可能有雜訊
B) 只記代碼、不通知
只在 turn_gate_decisions 寫 guard_exception，不告警（effort S、零額外實作、risk low）。✅ 不打擾 ✅ 事後可查 ❌ 無即時通知

## answered decision (S8)
Commitment comparison:
| Commitment | Source/approval or pending | Current | A | B |
|---|---|---|---|---|
| turn_outcomes／turn_gate_decisions 保留 | D12 只核准不存原文 | 未定 | 30 天，併入既有 3am maintenance（scripts/run_maintenance.py）清理 | 不設期限，列 TODO 之後再議 |
| 不存原文 | D12 approved | 不存 | 不存 | 不存 |

Question: CEO-D15 — S8: 兩個新紀錄檔要保留多久？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）spec review 第 3 輪發現。
ELI10: 這波會新增兩個紀錄檔：每輪結果（turn_outcomes）和每次判斷（turn_gate_decisions），都不存原文，一天大約一兩千筆。審查指出計畫寫了「保留 30 天」卻沒有你核准過。專案每天凌晨 3 點已經有維護作業在清其他紀錄，可以順便清這兩個檔。不設期限的話，檔案會一直長，但一年大約也只有幾十 MB。
Stakes if we pick wrong: 30 天的話，超過一個月的漏斗趨勢就看不到；不設期限則檔案持續長大，之後要再補清理。
Recommendation: A 保留 30 天 because D15 評估與 E4、E5 的觸發都只需要兩週到一個月的資料，沿用凌晨的維護作業不新增排程。
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) 保留 30 天 (recommended)
  ✅ 所有觸發條件（最長兩週）與 D15 評估都夠用
  ✅ 沿用凌晨 3 點既有維護作業，不新增排程
  ❌ 超過一個月的長期趨勢看不到，要另外彙總才留得住
B) 不設期限，之後再議
  ✅ 長期趨勢資料全留著，之後想分析隨時有
  ✅ 不用動維護作業
  ❌ 檔案持續長大，之後還是要回頭補清理規則
Net: 夠用就清 vs 全部留著之後再說。
Header: 紀錄保留
A) 保留 30 天 (recommended)
兩檔保留 30 天，於 scripts/run_maintenance.py 的 3am 作業加清理與測試（effort S、risk low）。✅ 夠用 ✅ 不新增排程 ❌ 長期趨勢要另彙總
B) 不設期限，之後再議
不清理，TODOS.md 加一條「紀錄檔保留期限」（effort S、零實作、risk low）。✅ 全留 ✅ 不動維護 ❌ 之後要補

### 0I 時序預演（CEO review；文件核准 CEO-D16=A 之後）
```
  第 1 小時（基礎）：實作者要先知道——PR#139/#140/barge-in 已合進 main（G10）；棘輪現值（G9：先量 _apply_wake_guards、handle_stt_result）；
                    VAD 回呼在 voice_recv 執行緒（D7、learning vad-callback-voice-recv-thread）；turn_key＝speaker+first_audio。
  第 2–3 小時（核心）：會卡的模糊處——TurnOutcome 的晚到欄位走 turn_update（G1）；只寫通過發現的輪（G2）；gate decision 去重優先序（G6）；
                    route 欄位命名（G4）；reason_code 提前到步驟 1 且淨 statement 不增（G7、G9）。
  第 4–5 小時（整合）：會意外的地方——WakeShortcut 提前 return 也要寫 outcome（OV-14）；Track A 已接、Track B 被 double_wake 擋的同句（G6）；
                    daily_user_needs.py 的「尚無資料」分支；sonnet 跑不了 venv 測試要由主 agent 跑（CLAUDE.md）。
  第 6 小時以後（打磨）：會希望先規劃的——跨執行緒測試範本（tests/test_barge_in_wiring.py）、mutation check 清單（G6 去重、G2 範圍、E3 公式）、
                    頂層模組 INFO 被吞（新模組要加 logger allowlist，learning new-toplevel-logger-needs-info-allowlist）。
```
可行性阻擋項（動工前必須成立）：G10 步驟 0 全部合進 main；G9 棘輪現值量測；G3 步驟 5 規則表（只擋步驟 5）。
待定選擇：無（S1–S8 已決）。
工時（本計畫 9 個 PR＋步驟 0）：human ~3 週／CC＋gstack ~2–3 天實作＋使用者人工分類 ~1h；照一天 1–2 個 PR 約 6–9 個工作天。

### CEO 審查章節 1–11（發現與處置；以下皆為已核准範圍的必要實作，無新選擇）

**Section 1 架構**
```
  sink 執行緒 ──bridge──▶ loop ─▶ TurnGate(純核心) ─▶ IntentBus ─▶ handler
                                   │   ▲                          │
                                   │   └─ RoomState / ConversationState（純資料，注入 now）
                                   ├─▶ TurnGateObserver（mixin，IO 殼）─▶ turn_gate_decisions.jsonl
                                   │                                   └▶ E2 Discord 訊息（send/edit、關窗計時器）
                                   └─▶ TurnOutcome writer（mixin）─▶ turn_outcomes.jsonl ─▶ daily_user_needs.py（E3）
```
- WARNING：E2 會讓 TurnGate 碰到 Discord IO。處置：TurnGate 只回傳事件（opened/closed/preempted），由 mixin 的 observer 做 send/edit 與計時（純核心＋IO 殼，記憶 design_disciplines_for_future_consumers）。
- 10x／100x：狀態是每 speaker 一份的小 dict；紀錄量 ×100 仍 <20 萬筆／天，jsonl 追加可承受。先壞的是 Discord 編輯訊息限流（E2），已有失敗吞掉記 log。
- SPOF：TurnGate 是發現階段唯一入口 → D8（守衛 fail-closed／窗口 fail-open）＋D14 kill flag＋CEO-D14 告警。
- 回滾：1／2／4／5 用 git revert＋重啟（約 5 分鐘）；3b／3c 改 env 重啟（約 1 分鐘）。
- 產出物價值檢查：turn_gate_decisions 由 E3 報表、E4 觸發、D18 稽核使用；turn_outcomes 由 E3、D15 評估使用；退場條件＝ConfirmPolicy 評估完成後再檢討欄位。

**Section 2 錯誤與救援**
| 路徑 | 可能出錯 | 例外 | 處置 | 使用者看到 |
|---|---|---|---|---|
| TurnOutcome／gate decision 寫檔 | 磁碟滿、權限 | OSError | 吞掉，logger.warning 每小時最多一次（含檔名與 turn_key） | 無 |
| E2 send/edit | 限流、訊息已刪、無權限 | discord.HTTPException／NotFound／Forbidden | 吞掉，logger.info（含 speaker、動作） | 少一則提示 |
| E2 `active_text_channel` 為 None | 未綁頻道 | — | 略過，logger.debug | 無 |
| 守衛判斷 | 任何例外 | Exception（D8 唯一允許的 catch-all，因要 fail-closed） | 擋下＋寫 guard_exception＋連續 3 次告警（CEO-D14） | 漏聽一句 |
| 窗口判斷 | 任何例外 | Exception | 當作沒窗口（D8），logger.warning | 要喊馬文 |
| 報表讀 jsonl | 壞行 | json.JSONDecodeError | 跳過該行並計數，報表顯示壞行數 | 無 |
| 報表讀檔 | 檔不存在 | FileNotFoundError | 顯示「尚無資料」 | 無 |
| maintenance 清理 | 檔被占用 | OSError | logger.warning，下次再清 | 無 |
GAP：0（catch-all 僅限 D8 兩處且皆有記錄與測試）。

**Section 3 安全**：無新對外介面、無新秘密、無新依賴。紀錄只含已同意者（voice_controller.py:1032 先擋）。E2 公開顯示的名字與既有文字鏡像相同。No issues found.

**Section 4 資料流與邊界**
- WARNING（非同步順序）：Track A 的 `handle_stt_result` 在 IntentBus `await` 期間，Track B 的同句可能進來。D7 單執行緒擋不住跨 await 的交錯。處置：TurnGate 的「檢查＋消費 expectation」必須是同步、中間不 await 的一個方法；回歸測試用可控的暫停點跑「A 先消費、B 後到」與「B 先到、A 後到」兩種順序，斷言只消費一次。
- WARNING（E2 計時器）：對談延長或被搶時，舊的關窗計時器可能晚到並把新訊息編成已結束。處置：每次開窗帶 epoch，計時器觸發時 epoch 不符就不動作；補測試。
- 互動邊界：重複喚醒（Double Wake 擋）、窗內連講兩句（第二句不重複開窗）、空字串 STT（現有幻覺守衛先擋）。皆有既有處置。

**Section 5 程式品質**：TurnGate 的判斷寫成「有序的守衛／窗口述詞表」逐一套用，不寫成一長串 if（避免單一方法超過 5 個分支，也對齊棘輪）。新模組命名 turn_gate／conversation_state／turn_outcome 描述用途。No other issues.

**Section 6 測試**
```
 新東西                         | 測試類型      | 失敗情境測試                  | 邊界測試
 TurnOutcome＋turn_update        | 單元          | 寫檔 OSError                  | Track A/B 同 turn、文字輸入
 gate decision＋去重             | 單元          | 守衛例外                      | accept>block>ignore、preempt 不去重
 TurnGate 守衛／窗口             | 單元＋情境表  | 每道守衛注入例外（chaos）     | 每窗口 ttl 前後一刻（注入 now）
 跨 await 消費                   | 單元（暫停點）| —                             | 兩種完成順序
 E2 observer                     | 單元（假 channel）| HTTPException/None 頻道   | 延長、被搶、epoch 不符
 kill flag 2×2                   | 單元          | —                             | 四種組合副作用只一次
 E3 報表                         | 單元          | 壞行、缺檔                    | 「尚無資料」各欄
 maintenance 30 天清理           | 單元          | 檔被占用                      | 第 30／31 天邊界
```
- 2am 星期五的信心測試：情境表對舊實作與新實作都全綠＋kill flag 退回測試。
- 惡意 QA：Track A/B 交錯＋守衛例外同時發生。
- 混沌測試：每道守衛輪流注入例外，斷言 fail-closed 且告警計數正確。
- 防 flaky：所有 TTL 一律注入 `now`（沿用 WakeSession 的寫法），不用真的 sleep。
- LLM／prompt 變更：無（修正沿用既有 resolver）。

**Section 7 效能**：TurnGate 每句是純 CPU 判斷（微秒級）；jsonl 同步追加每筆 <1ms、每天 1–2 千筆；E2 每天約 15–20 次開窗（WakeSession「聆聽中」10/20–22 三天 43 次，CEO Outside Voice 更正），即每天 30–40 則 send/edit（見 V4）；報表讀 30 天約 6 萬行。No issues found.

**Section 8 可觀測性**
- WARNING：turn_gate／conversation_state／turn_outcome 若是頂層模組，INFO log 會被 `setup_early_logging` 吞掉（learning new-toplevel-logger-needs-info-allowlist）。處置：新模組加進 INFO allowlist，並補 tests/test_main_discord_logging.py。
- 指標：E3 每日漏斗；告警：CEO-D14；除錯：turn_gate_decisions 保留 30 天＋stt_history 原文。
- Runbook（新增）：守衛疑似搬錯 → `launchctl setenv` 不適用，改在 `.env` 設 `MARVIN_TURN_GATE_GUARDS=0`（或 WINDOWS=0）後 `launchctl kickstart -k gui/$(id -u)/com.antigravity.marvin.bot`；查 `records/turn_gate_decisions.jsonl` 的 guard_exception 與 bot_main.log。

**Section 9 部署**：無 DB migration。每個 PR 部署＝主目錄切 commit＋kickstart 重啟；遵守「晚間 20–24 非緊急不重啟」（記憶 feedback_no_evening_restarts），重啟前先確認台上無人。部署後 5 分鐘：啟動 log 無 Traceback、turn_outcomes／turn_gate_decisions 開始增長；1 小時：有喚醒時 E3 數字非零、無 guard_exception 告警。

**Section 10 長期走向**：可逆性 4/5（步驟 2 刪程式靠 revert，3/5；其餘有 flag 或易 revert）。技術債：3a 薄轉接與兩個 flag 到步驟 6 才清；WakeSession 名稱在 ConversationState 取代後退場。之後的路：ConfirmPolicy（TODO，步驟 5 後 7 天評估）、E4、E5、對談快速通道（TODOS P1）都會以 TurnGate 為單一接入點。

**Section 11 設計（E2 文字提示）**
```
 未顯示 ──開窗(對談窗/房間窗)──▶ 「🎧 聽 X 中」 ──關窗計時器(epoch 相符)──▶ 「🎧 已結束」
    ▲                               │  └─被搶──▶ 舊訊息「🎧 已結束」＋新 target 新訊息
    └──── send 失敗／無頻道（略過＋log）
```
狀態：載入（無）、空（不顯示）、錯誤（略過）、成功（顯示）、部分（被搶）。UI 範圍很小，不需要 /plan-design-review。

### CEO Outside Voice 補註（Claude Plan 子代理 native fallback；已核准範圍的釐清與事實更正，不另議）
O2. **窗口與守衛優先序**（現行行為，R7 保留）：先跑守衛，再判窗口；WakeSession／followup 窗口命中時可覆蓋 Echo、Response Lock、Storm、Double Wake 的擋下（voice_controller.py:1501）。例外：守衛例外（guard_exception）**不可被窗口覆蓋**（D8 本意是不讓 Marvin 自己觸發自己）。守衛狀態的外部讀寫點一併列入 3c 搬遷與 G8 負責表：Gate 拒絕解鎖 Response Lock（:2946）、風暴計數重設（:2833）、`process_debounced_speech` 讀 `last_wake_time` 的 5 秒防重（:1775）。3a-1 的薄轉接要把「守衛結果」與「窗口覆蓋」拆成兩步回傳，這是允許的最小行為面改動（語意不變）。
O3. **TurnGate 範圍外的發現路徑**：`process_debounced_speech` 的免喚醒直派（音樂指令／找歌／音樂資訊，:1784 起）、game mode、`car_http_app.py:65` 文字入口、Semantic ETD 強制結算重入（:1591）。discovered_by 補 `wakeless`、`game`、`car_text`、`etd_forced`；這些路徑也寫 TurnOutcome（G2 範圍擴為「有派發的輪」）；route 涵蓋這些路徑，步驟 2／G13 才看得到。
O5. **被丟棄的喚醒**：入隊後被 Stale Drop、Gate 拒絕（:2946）、speaker_dispatch 取代的輪，在丟棄點寫 TurnOutcome `executed=false`＋`route=dropped:<原因>`，覆蓋率與執行率才正確。
O6. **reason_code 取第一個命中的守衛**（依現行判斷順序）；3 秒 per-speaker 守衛的 log 由 debug 改 info，步驟 1 數字才能對帳。
O7. **沒有真實樣本的守衛**：Global Wake Guard（16 天 0 次）以合成樣本補足並註明；TTS 冷卻分支視 V2 決定。
O8. **步驟 2 觀察期**（更正 G11，G11 是作者補註非使用者核准）：16 天既有 log 交叉比對＋步驟 1 上線後至少 7 天 route 計數，兩者都為 0 才列入刪除清單。
O11. **步驟 1 的棘輪重構**另成 PR 內獨立 commit（先抽等量程式、再加紀錄），驗收分開。

## answered decision (V1)
Commitment comparison:
| Commitment | Source/approval or pending | Current | A | B | C | D |
|---|---|---|---|---|---|---|
| Response Lock 解鎖時機 | 1b790f9 前：TTS 送達即解鎖 | 只在 30 秒逾時或 Gate 拒絕 | TTS 送達即解鎖（步驟 1 前獨立小 PR，含紅測） | 維持現行，情境表凍結 | 限 1 小時調查後再決定 | 延後此提議（現行不變） |
| R7 保留行為 | D11 approved | — | 以修正後為準 | 以現行為準 | 待定 | 待定 |

Question: CEO-D17 — V1: 要不要先修好「Marvin 回完話後 30 秒內別人喊不醒」的 bug？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）CEO 外部意見第 1 條。
ELI10: 有一道叫 Response Lock 的守衛：Marvin 接受一次喚醒後，會先擋住其他喚醒，等回話送出去再解鎖。6/17 那次重構把「送出去就解鎖」那一行刪掉了，現在只會在 30 秒後才解。所以每次有人叫 Marvin，接下來 30 秒內別人喊馬文都會被擋，16 天擋了 66 次；連 Marvin 問完問題的追問窗也因此幾乎接不到。這直接傷到「順利聊天」。如果不修，這次重構的測試會把這個 bug 當成「正確行為」保留下來。
Stakes if we pick wrong: 不修的話，朋友接連叫馬文會一直被吞，而且重構後被測試鎖死；修的話要多一個小 PR，並確認解鎖後不會冒出回授誤觸。
Recommendation: A 先獨立修 because 這是線上真的 bug、直接影響使用者說的順利聊天；先修再做 3a 的情境表，才不會把 bug 凍結成規格。
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) 步驟 1 前獨立小 PR 修好 (recommended)
  ✅ 回完話就解鎖，別人接著叫馬文不會再被吞
  ✅ Marvin 問完問題的追問窗重新接得到，直接改善順利聊天
  ❌ 多一個小 PR，且解鎖後要觀察有沒有回授誤觸
B) 維持現行，凍結進情境表
  ✅ 不改任何現行行為，重構風險最低
  ✅ PR 數不變
  ❌ 30 秒擋人的 bug 被測試鎖死，之後更難修
C) 先調查再決定（限 1 小時）
  ✅ 確認 6/17 刪掉是不是有意的，再決定
  ✅ 只讀程式碼與 log
  ❌ 拖延，而 bug 一直在線上
D) 延後此提議
  ✅ 這次審查先不處理
  ✅ 不影響其他已核准的決定
  ❌ 3a 情境表會照現行（含 bug）寫
Net: 先修真 bug vs 不動現狀 vs 先查 vs 擱著。
Header: ResponseLock
A) 步驟 1 前獨立小 PR 修好 (recommended)
補回「TTS 送達即解除 Response Lock」，先寫重現的紅測，部署後看 Response Lock 壓抑次數下降、無回授誤觸；R7 情境表以修正後為準（effort S、risk medium）。✅ 不再吞人 ✅ 追問窗復活 ❌ 多一個 PR
B) 維持現行，凍結進情境表
不修，情境表以現行為準（effort S、零實作、risk low）。✅ 不動行為 ✅ PR 不變 ❌ bug 被鎖死
C) 先調查再決定（限 1 小時）
只讀調查 1b790f9 刪除原因與回授風險，回來再決定；期間現行不變（effort S、risk low）。✅ 先確認 ✅ 唯讀 ❌ 拖延
D) 延後此提議
本次不處理，現行不變（effort S、零實作、risk low）。✅ 不動 ✅ 不影響其他 ❌ 情境表照 bug 寫

## answered decision (V2)
Commitment comparison:
| Commitment | Source/approval or pending | Current | A | B | C | D |
|---|---|---|---|---|---|---|
| TTS 結束後 2 秒回授冷卻 | 1b790f9 前存在；現為死碼（只初始化 :473） | 從不生效（約 4 個月） | 與 V1 同 PR 補回（含紅測） | 步驟 2 刪除這條死碼分支與欄位 | 限 1 小時查 6/17 後有無自我觸發事件再決定 | 延後（死碼保留） |
| V1 Response Lock 解鎖 | CEO-D17 approved | — | 不變 | 不變 | 不變 | 不變 |

Question: CEO-D18 — V2: 從 6/17 起就沒在作用的「Marvin 講完 2 秒內不接喚醒」要補回來，還是乾脆刪掉？
Project/branch/task: Marvin 意圖漏斗簡化（docs/designs/intent-funnel-simplification.md）CEO 外部意見第 1 條（後半）。
ELI10: 原本設計是 Marvin 每講完一句，接下來 2 秒不接喚醒，避免把自己聲音的尾巴當成有人叫它。6/17 重構把設定這 2 秒的那行刪掉了，所以這 4 個月它其實都沒作用。Discord 每個人的聲音是分開收的，Marvin 自己的聲音通常不會混進來，這 4 個月也沒有查到「講完被自己叫醒」的紀錄。補回來會讓「Marvin 剛講完、朋友馬上接著叫它」被擋 2 秒，對順利聊天反而不利。
Stakes if we pick wrong: 補回來可能吞掉緊接著的叫喚；刪掉的話，萬一真有回授尾巴就少一道防線（但這 4 個月本來就沒有）。
Recommendation: B 刪掉這條死碼 because 它已經 4 個月沒作用也沒出事，Discord 分軌收音讓回授尾巴很少見；留著死碼會讓情境表要湊假樣本、測試看起來在守一個不存在的行為。
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) 跟 V1 一起補回
  ✅ 回到原本設計，多一道防回授尾巴的保險
  ✅ 跟 V1 同一個小 PR，不多開 PR
  ❌ Marvin 剛講完的 2 秒內朋友叫它會被擋，跟順利聊天方向相反
B) 刪掉這條死碼 (recommended)
  ✅ 程式與情境表都不用守一個不存在的行為，O7 不用湊假樣本
  ✅ 維持這 4 個月實際在跑、沒出事的行為
  ❌ 萬一某人用外放喇叭沒開回音消除，少一道防線
C) 先調查再決定（限 1 小時）
  ✅ 確認 6/17 之後真的沒有自我觸發事件再決定
  ✅ 只讀 log 與程式碼
  ❌ 多花時間，結論大概率相同
D) 延後此提議
  ✅ 這次先不處理
  ✅ 不影響 V1
  ❌ 死碼繼續留著，情境表要湊合成樣本
Net: 補回保險但可能吞人 vs 刪掉死碼維持實際行為。
Header: 回授冷卻
A) 跟 V1 一起補回
TTS 結束時設 `_tts_echo_cooldown_until = now + 2.0`，與 V1 同 PR、含紅測（effort S、risk medium）。✅ 多一道保險 ✅ 不多開 PR ❌ 可能吞緊接的叫喚
B) 刪掉這條死碼 (recommended)
步驟 2 一併刪除 `_tts_echo_cooldown_until` 欄位與 Echo Guard 中的冷卻分支，情境表不收這條（effort S、risk low）。✅ 不守假行為 ✅ 維持實際行為 ❌ 少一道防線
C) 先調查再決定（限 1 小時）
只讀查 6/17 後的自我觸發紀錄，回來再決定；期間死碼不動（effort S、risk low）。✅ 先確認 ✅ 唯讀 ❌ 多花時間
D) 延後此提議
本次不處理（effort S、零實作、risk low）。✅ 不動 ✅ 不影響 V1 ❌ 死碼留著

### CEO Outside Voice 決定後的規格調整（CEO-D17～D21）
- **步驟 0.5（新）**：Response Lock 修正小 PR（CEO-D17）。驗收：重現紅測（接受喚醒→TTS 送達→另一人立刻喊馬文應被接受）；部署後 24h Response Lock 壓抑次數下降且無回授誤觸；在步驟 1 之前合併。
- **步驟 2** 加刪除 TTS 回授冷卻死碼（CEO-D18）；O7 不再需要為它湊樣本。
- **步驟 5** 開工門檻（CEO-D19）：一週人工抽樣修正句 ≥5 次；不足則移入 TODOS.md。
- **E2**（CEO-D20）：一則固定狀態訊息只編輯；取代補充第 9 條的「被搶時舊訊息編為已結束、新 target 發新訊息」→ 改為直接把同一則改成新 target；epoch 規則改為「計時器只在狀態仍是同一窗口時改成『沒有在聽』」；Section 7 的 30–40 則訊息改為每天 30–40 次編輯、0 則新訊息。
- **kill flag**（CEO-D21）：只支援 (1,1)、(0,0)；混合組合啟動時 logger.error 並走 (0,0)；G8 表與「四種組合各有測試」改為兩種組合＋混合組合被拒的測試。
- **PR 計數**：本計畫 PR＝0.5、1、2、3a-1、3a-2、3b、3c、4、5（可能延後）、6 共 10 個；步驟 0 的既有 PR 另計。

Approval readiness: PASS — CEO ledger C1（CEO-D1）、E1（CEO-D3，經 CEO-D8 修訂）、E2（CEO-D4，經 CEO-D9、CEO-D20 修訂）、E3（CEO-D5）、E4（CEO-D6 延後）、E5（CEO-D7 延後）、S1～S8（CEO-D8～D15）、文件核准（CEO-D16）、V1～V5（CEO-D17～D21）；工程 ledger 的 D2 由 D15 取代、D9 由 D14 取代、D14 由 CEO-D21 部分取代、D3 加上 CEO-D19 門檻。


## CEO Review 輸出

### Reality rows（CEO）
見「REALITY rows（CEO）」小節的 7 行；`PLAN_REALITY: phase=ceo rows=7/7 missing=none unverified=5`（工具取檔內第一個 premise 行；CEO 自己的 premise-table 為 unverified=2）。

### NOT in scope
- 延後（已進 TODOS.md）：E4 多人同時對談（CEO-D6）、E5 修正延伸成撤銷（CEO-D7）、ConfirmPolicy（D15，觸發改為步驟 5 上線滿 7 天或步驟 5 確定延後）。
- 不併入本計畫：對談延遲快速通道（TODOS P1，CEO-D1）；Echo Guard 誤擋修正（D18，只抽樣，>10% 才另開）；`_QUESTION_MARKER_RE` 加「吧」（TODOS.md:86 (b)）；IntentBus forwarding stubs 清理（TODOS.md:53）。
- 條件延後：步驟 5 若一週抽樣修正句 <5 次則移入 TODOS.md（CEO-D19）。

### What already exists
WakeSession（被 ConversationState 取代，測試依 D11 改寫退役）；pipeline_timing 的跨佇列轉送（TurnOutcome 沿用方式）；IntentBus `_resolve_and_redispatch`（修正沿用）；NemoClawAgent（D10 併入）；ErrorDispatcher（CEO-D14 告警沿用）；scripts/daily_user_needs.py（E3 擴充）；scripts/run_maintenance.py（CEO-D15 清理）；`active_text_channel`（E2 狀態訊息）。

### Dream state delta
```
  現在                                   本計畫完成後                              12 個月理想
  6 套窗口散落、30 秒內別人喊不醒、      單一 TurnGate＋每輪紀錄＋判斷理由；       不用喊名字也自然插話、
  修正要整句重講、看不到 Marvin 在聽誰   Response Lock 修好；看得到在聽誰；         回話 <1 秒、
                                         「不是，是 Y」可改（若需求達門檻）          說錯一句就能改
```
剩下的差距：回話速度（TODOS P1）、不用喊名字的判斷模型（需 E1 資料＋人工標註）、多人同時對談（E4）。

### Error & Rescue Registry
見「CEO 審查章節 1–11」Section 2 表（8 列，0 個 CRITICAL GAP）。

### Failure Modes Registry
```
  CODEPATH              | FAILURE MODE              | RESCUED? | TEST? | USER SEES?          | LOGGED?
  ----------------------|---------------------------|----------|-------|---------------------|--------
  sink 執行緒入口        | 非 loop 建 task           | Y(bridge)| Y     | 無（已修 cc617b1）   | Y
  Response Lock         | 送達後不解鎖（現行 bug）    | Y(步驟0.5)| Y    | 30 秒內喊不醒→修正後無 | Y
  守衛判斷              | 例外                      | Y(fail-closed)| Y | 漏聽一句；owner 收 DM | Y
  窗口判斷              | 例外                      | Y(當作沒窗口)| Y  | 要喊馬文            | Y
  Track A/B 同句        | 跨 await 重複消費          | Y(同步消費)| Y   | 無                  | Y
  E2 狀態訊息           | 限流／訊息被刪／無頻道      | Y(吞掉)  | Y     | 狀態暫時不更新       | Y
  紀錄寫檔              | OSError                   | Y(吞掉)  | Y     | 無                  | Y(每小時一次)
  報表                  | 壞行／缺檔                 | Y        | Y     | 「尚無資料」         | Y
  3b/3c 換上            | 搬錯                      | Y(kill flag (0,0))| Y | 改 env 重啟即退回 | Y
```
CRITICAL GAP：0。

### Scope Expansion Decisions
- Accepted：E1 判斷紀錄（CEO-D3→CEO-D8）、E2 在聽誰（CEO-D4→CEO-D9→CEO-D20）、E3 每日漏斗（CEO-D5）。
- Deferred：E4（CEO-D6）、E5（CEO-D7）。
- Skipped：無。

### Diagrams
1. 系統架構：見「執行規格定稿」架構圖與 Section 1 圖。
2. 資料流（含陰影路徑）：
```
 STT 結果 ─▶ turn_key ─▶ TurnGate.decide(sync) ──accept──▶ IntentBus ─▶ handler ─▶ TurnOutcome(主紀錄)
   │ nil/空字串：幻覺守衛先擋     │ block：gate decision(block, guard_*)        └▶ 背景例外 → turn_update(executed=false)
   │ 文字輸入：turn_key=speaker+收到時間 │ 例外：守衛→block(guard_exception)／窗口→當作沒窗口
   └ 重複（A/B）：同 turn_key，accept>block>ignore    └ 入隊後丟棄 → TurnOutcome(executed=false, route=dropped:*)
```
3. 狀態機（對談窗）：
```
 CLOSED ──喚醒被接受──▶ ARMED ──Marvin 回話──▶ OPEN ──idle 120s／結束語／音樂指令──▶ CLOSED
   ▲                      │                     │  └──另一人新喚醒──▶ CLOSED（舊）＋ARMED（新），寫 preempt
   └──30s 未回話（stale）──┘                     └──barge-in──▶ OPEN（barge_in_pending）
 不可能的轉移：CLOSED→OPEN（必經 ARMED）、OPEN 期間第二個 target（單一 target，E4 延後）
```
4. 錯誤流：守衛例外 → block＋guard_exception → 同守衛連續 3 次 → ErrorDispatcher（incident＋DM owner）。
5. 部署順序：步驟 0（#139→#140→barge-in 等）→ 0.5（Response Lock）→ 1 → 2 → 3a-1 → 3a-2 → 3b → 3c → 4 → 5（門檻）→ 6；每步：主目錄切 commit → 避開 20–24 → kickstart → 5 分鐘／1 小時檢查。
6. 回滾：
```
 出事 ─▶ 是 3b/3c？ ──是──▶ .env 設 MARVIN_TURN_GATE_WINDOWS=0 與 GUARDS=0（只支援同值）→ kickstart（~1 分）
            └──否──▶ git revert 該 PR → 主目錄切回 → kickstart（~5 分）
```

### Stale Diagram Audit
- §3「目標架構」與「審查核准版計畫」架構圖：已被「執行規格定稿」取代（各節已標註），不再準確。
- 計畫會動到的程式檔（voice_controller*.py、wake_session.py、intent_bus.py、discord_voice_engine.py）內沒有 ASCII 圖。
共 2 張過期（已標註取代）。

### Implementation Tasks（CEO 審查後完整清單）
- [ ] **T0 (P1, human: ~1 天 / CC: ~30min)** — 步驟 0 — #139、#140、barge-in 與其餘 prod commit 合進 main（G10）。Verify：main 含 cc617b1；CI（workflow CI）綠。
- [ ] **T0.5 (P1, human: ~2h / CC: ~15min)** — Response Lock — 補回 TTS 送達即解鎖（CEO-D17）。Files：cogs/voice_controller_playback.py、tests/。Verify：重現紅測；部署後 24h 壓抑次數下降、無回授誤觸。
- [ ] **T1 (P1, human: ~1 天 / CC: ~45min)** — TurnOutcome＋守衛類 gate decision＋E3 第一階段（含 O3、O5、O6、G1、G2、G4、G9、O11）。Verify：PR 1 驗收＋棘輪。
- [ ] **T2 (P1, human: ~半天 / CC: ~20min)** — 刪零流量路徑＋TTS 冷卻死碼（O8、CEO-D18）。Verify：16 天 log＋7 天 route 皆 0；pytest 綠。
- [ ] **T3a1 (P1, human: ~1 天 / CC: ~1h)** — 介面＋薄轉接（守衛結果與窗口覆蓋拆兩步，O2）。
- [ ] **T3a2 (P1, human: ~2 天 / CC: ~3h＋人工 ~1h)** — 測試改寫＋情境表＋Echo Guard 分類（Response Lock 以修正後為準）。
- [ ] **T4 (P1, human: ~2 天 / CC: ~1.5h)** — 3b 窗口＋E2 固定狀態訊息（CEO-D20）＋WINDOWS flag。
- [ ] **T5 (P1, human: ~2 天 / CC: ~1.5h)** — 3c 守衛＋外部讀寫點（O2）＋CEO-D14 告警＋同值 flag（CEO-D21）。
- [ ] **T6 (P2, human: ~2 天 / CC: ~1h)** — bus 收編（G13 範圍）＋winner_agent/bid。
- [ ] **T7 (P2, human: ~1 天 / CC: ~30min)** — 修正接 last_intent（先過 CEO-D19 門檻；G3 規則表）。
- [ ] **T8 (P3, human: ~1h / CC: ~10min)** — 清理舊路徑與 flag（G12 穩定條件）。

### Unresolved Decisions
無。

### Completion Summary
```
  +====================================================================+
  |            MEGA PLAN REVIEW — COMPLETION SUMMARY                   |
  +====================================================================+
  | Mode selected        | SCOPE EXPANSION                             |
  | System Audit         | 0 對 Marvin 不滿（14 天）；延遲 6.1s 是體感痛點；|
  |                      | 外部意見挖出 Response Lock 6/17 起不解鎖     |
  | Step 0               | C1 照工程版全做；E1–E3 採納、E4/E5 延後       |
  | Section 1  (Arch)    | 1 issues found                              |
  | Section 2  (Errors)  | 8 error paths mapped, 0 GAPS                |
  | Section 3  (Security)| 0 issues found, 0 High severity             |
  | Section 4  (Data/UX) | 2 edge cases mapped, 0 unhandled            |
  | Section 5  (Quality) | 1 issues found                              |
  | Section 6  (Tests)   | Diagram produced, 0 gaps                    |
  | Section 7  (Perf)    | 0 issues found（E2 量經外部意見更正）         |
  | Section 8  (Observ)  | 1 gaps found                                |
  | Section 9  (Deploy)  | 1 risks flagged                             |
  | Section 10 (Future)  | Reversibility: 4/5, debt items: 2           |
  | Section 11 (Design)  | 0 issues                                    |
  +--------------------------------------------------------------------+
  | NOT in scope         | written (7 items)                           |
  | What already exists  | written                                     |
  | Dream state delta    | written                                     |
  | Error/rescue registry| 8 rows, 0 CRITICAL GAPS                     |
  | Failure modes        | 9 total, 0 CRITICAL GAPS                    |
  | TODOS.md updates     | 2 items proposed（E4、E5）＋1 觸發更新        |
  | Scope proposals      | 5 proposed, 3 accepted (EXP)                |
  | CEO plan             | written                                     |
  | Outside voice        | codex 未安裝 → native Claude completed（12 條；5 決策、7 補註）|
  | Lake Score           | 1/1 recommendations chose complete option   |
  | Diagrams produced    | 6 (arch, data flow, state, error, deploy, rollback) |
  | Stale diagrams found | 2                                           |
  | Unresolved decisions | 0                                           |
  | Reality rows         | 7/7 rows (PLAN_REALITY), 2 unverified       |
  | Spec review          | 3 輪皆 6/10；第 3 輪修正 15 of 17 自核，spec not re-verified |
  +====================================================================+
```

## Eng Review 第 2 輪（CEO 後重跑，/plan-eng-review，2026-10-22）

審查目標（固定）：`docs/designs/intent-funnel-simplification.md` 的「## 執行規格定稿（CEO review 後）」＋定稿補充 1–17、G1–G13、O2–O11、CEO-D17～D21。比對基準：origin/main 9a03386（含 cc617b1、fab74a1、c04be3d）。

### 範圍紀錄（Scope Challenge B，第 2 輪）
feature answers: 無新的刪減提議（E1–E3 由 CEO-D3/D4/D5 核准、E4/E5 延後、ConfirmPolicy 延後，皆不重議）；structure: A（沿用 D4 Original arrangement；CEO 加的 TurnGateObserver mixin、gate decision 寫入、E3 報表擴充屬 CEO 核准功能的必要檔案，不另議）；accepted scope: 定稿 PR 0.5、1、2、3a-1、3a-2、3b、3c、4、5（CEO-D19 門檻）、6；pending remedies: R12、R13

複雜度：新模組 turn_gate／conversation_state（含 RoomState）／turn_outcome＋TurnGateObserver mixin＋約 5 個新 agent，改動檔案 >8 [estimated: 定稿 Files 欄]，觸發複雜度門檻；以 D4 既有答案解決，未重問。

### Scope Challenge 事實更正（第 2 輪，不改行為）
- SC2-1 **步驟 0／0.5 已完成**：`git merge-base --is-ancestor` 確認 cc617b1、fab74a1 皆在 origin/main（PR#141 merge 9a03386）。T0、T0.5 標為完成。
- SC2-2 **步驟 0.5 實作與定稿描述不同**：定稿 T0.5 寫「TTS 送達即解鎖、Files：voice_controller_playback.py」；實際 fab74a1 是「佇列處理結束 finally 解鎖」，在 `cogs/voice_controller_wake_session.py:66-84`（`_release_response_lock`／`_process_query_task_locked`／`_release_lock_after`），回應在 `_process_queued_query` 內 await `_stream_response`（voice_controller.py:3128），所以解鎖時間點約等於回話播完。3a-2 情境表的 Response Lock 以「處理結束解鎖」為準。
- SC2-3 **O2 守衛狀態外部讀寫點補列**（3c 搬遷清單與 G8 負責表必須含）：`_release_response_lock`（wake_session mixin :66）、WakeShortcut 直派包 `_release_lock_after`（voice_controller.py:1274）、`_followup_override` 讀 Response Lock 決定問句窗能否接走（wake_session mixin :134：`if not self._wake_response_pending and fusion is not None and fusion.is_open()`）。O2 原列 :2946、:2833、:1775 仍有效。
- SC2-4 **棘輪現值** [measured: core_size_metrics.method_statements，origin/main 9a03386]：`handle_stt_result` 183／187、`_apply_wake_guards` 79／79（0 餘裕）、`_process_query_task` 21／40。G9 成立：步驟 1 要先從 `_apply_wake_guards` 抽出等量程式才放得下 reason_code。
- SC2-5 **TTS 回授冷卻死碼多一個讀取點**：除 Echo Guard（:1457）外，voice_controller.py:2437 也讀 `_tts_echo_cooldown_until`；步驟 2（CEO-D18）刪除時一併處理。

### Decision ledger（第 2 輪）

### R12: E2「在聽誰」提示顯示在哪裡
Finding: ARCH2-1，P2，confidence 8/10，main_discord.py:350（`if vc_cog is not None and os.getenv("MARVIN_TRANSCRIPT_MIRROR", "1") != "0":`）＋transcript_mirror.py:61-75（每 1.5 秒把累積行 send 成新訊息到 `active_text_channel`），reviewer：Claude（eng 第 2 輪）
Plan baseline: CEO-D20 approved —「一則固定狀態訊息只編輯」，顯示在 `active_text_channel`（定稿補充第 9 條），epoch 規則「計時器只在狀態仍是同一窗口時改成『沒有在聽』」
Runtime evidence: 對話鏡像 10/21 上線、預設開，與 E2 同一個頻道；每次喚醒至少貼一則（🎙️ 原話），有回答再貼一則（🤖）。固定訊息在幾輪對話後就被推出畫面。`change_presence` 目前全專案無人使用（grep 0 筆）。
Comparison grid:
| 選擇 | 現值 | A | B | C |
|---|---|---|---|---|
| R12 E2 顯示位置 | CEO-D20：頻道固定訊息、只編輯 | bot 狀態欄（presence）「🎧 聽 X 中」，關窗清掉 | 維持 CEO-D20 固定訊息 | 鏡像開著時在鏡像流貼「🎧 開始聽 X／🎧 結束」；鏡像關時用 CEO-D20 固定訊息 |
| 哪些窗口顯示 | CEO-D9 approved：WakeSession 類＋D5 房間窗 | 不變 | 不變 | 不變 |
| `WINDOWS=0` 不顯示 | 定稿行為規則 approved | 不變 | 不變 | 不變 |
| 失敗吞掉記 log | 定稿 Section 2 approved | 不變（例外改為 presence 更新失敗） | 不變 | 不變 |
| R13 與對談快速通道的先後 | pending | pending | pending | pending |
Question D2:
D2 — E2「在聽誰」提示會被對話鏡像洗掉，要換地方顯示嗎？
Project/branch/task: 意圖漏斗 3b 的 E2（CEO-D4→D9→D20），比對 origin/main 9a03386。
ELI10: CEO 審查決定在文字頻道放一則固定訊息「🎧 聽 X 中」，只編輯不另發。可是 10/21 上線的對話鏡像（transcript_mirror.py，預設開，main_discord.py:350）會把每句原話和 Marvin 的回答貼成新訊息，而且貼在同一個頻道。有人喊馬文時鏡像至少多一則，那則固定訊息講沒幾句就被推上去看不到，大家想知道「現在能不能直接講」的時候剛好看不到。
Stakes if we pick wrong: 照現案做，3b 要多養一個訊息物件、epoch 計時器和 5 個邊界測試，換到的提示在鏡像開著時幾乎沒人看得到。
Recommendation: A because 狀態欄一直看得到、不跟鏡像搶頻道，還能拿掉訊息 id 存記憶體、重啟殘留、編輯失敗這幾個邊界
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) 改用 bot 狀態欄 (recommended)
  ✅ 成員列表上 Marvin 的狀態一直看得到，不會被鏡像或聊天洗掉
  ✅ 不用存訊息物件，補充第 9 條重啟殘留與 epoch 編輯規則大多可刪，3b 變小
  ❌ 手機要拉開成員列表才看得到；改了 CEO-D20 選的顯示位置
B) 維持 CEO-D20 固定訊息
  ✅ 不改已核准規格，3b 照原案做
  ✅ 鏡像本來就是「穩定前」暫開，關掉後提示就看得到
  ❌ 鏡像開著期間這則提示幾乎沒用，卻要付全部實作與測試成本
C) 鏡像開時貼進鏡像流
  ✅ 「開始聽／結束」跟對話內容排在一起，看得出時序
  ✅ 鏡像開或關都有用
  ❌ 兩套顯示路徑要各自測，3b 多一份工
Net: 換到一直看得到的狀態欄、但手機不明顯，還是守住已核准的頻道訊息、接受鏡像開著時看不到。
Header: E2 位置
Options:
A) 改用 bot 狀態欄 (recommended)
開窗時 `change_presence` 顯示「🎧 聽 X 中」（房間窗「🎧 聽大家回答中」），關窗清掉；不發頻道訊息、不存訊息 id；presence 更新失敗吞掉記 log。取代 CEO-D20 與補充第 9 條的訊息／epoch 規則（human ~半天 / CC ~20min，risk low，維護最小）。
B) 維持 CEO-D20 固定訊息
照定稿做：頻道一則固定訊息只編輯、epoch 規則、補充第 9 條邊界測試；鏡像開著時會被洗掉（human ~1 天 / CC ~40min，risk low）。
C) 鏡像開時貼進鏡像流
`MARVIN_TRANSCRIPT_MIRROR` 開時，開／關窗各在鏡像流貼一行；鏡像關時走 CEO-D20 固定訊息；兩條路徑各自有測試（human ~1.5 天 / CC ~1h，risk low，維護兩套）。

State: approved
Actual answer: A) 改用 bot 狀態欄（使用者 2026-10-22，D2）
Accepted scope: 3b 的 E2 改為開窗時 `change_presence` 顯示「🎧 聽 X 中」（房間窗「🎧 聽大家回答中」），關窗清掉；不發頻道訊息、不存訊息 id；presence 更新失敗吞掉記 log。取代 CEO-D20 與定稿補充第 9 條的訊息／epoch 規則；CEO-D9 的顯示窗口範圍、`WINDOWS=0` 不顯示、失敗吞掉記 log 不變。
History: 無

### R13: 意圖漏斗與「對談極速通道」（TODOS P1）誰先動對談窗
Finding: ARCH2-2，P2，confidence 7/10，TODOS.md:8（「當喊了『馬文』喚醒詞或進入 Follow-up 視窗後…(1) Bypass Cleaner LLM…(4) 對談期間動態縮短 VAD silence 至 300ms」），reviewer：Claude（eng 第 2 輪）
Plan baseline: CEO-D1 approved —「對談延遲快速通道不併入本計畫」；先後順序未定。Section 10 寫快速通道日後以 TurnGate 為單一接入點。
Runtime evidence: 快速通道的 (1)(4) 都以「目前是不是在對談窗」為條件，這正是 3b 要從 WakeSession／fusion 窗搬進 ConversationState 的狀態。VAD 在 voice_recv 執行緒跑（learning vad-callback-voice-recv-thread），(4) 會從非 loop 執行緒讀窗口狀態。快速通道計劃書 docs/PLAN_fast_followup_conversation.md 未經審查，記憶 project_fast_followup_routing 記錄其延遲基準與位置有誤（瓶頸是路由串行，cleaner 在 ETD）。
Comparison grid:
| 選擇 | 現值 | A | B | C |
|---|---|---|---|---|
| R13 先後與接點 | 未定（CEO-D1 只說不併入） | 漏斗先做到 3b，快速通道之後只讀 ConversationState | 快速通道先做，3a-2 情境表把它當現行行為凍結、3b 一起搬 | 兩邊並行：先在 WakeSessionMixin 加一個唯讀查詢 `in_conversation_window(speaker, now)`，快速通道只准用它；3b 換實作時只改這個函式 |
| R12 E2 顯示 | D2 approved：bot 狀態欄 | 不變 | 不變 | 不變 |
| 快速通道本身的內容與基準 | 未審查 | 不在本題（另做 eng review） | 不在本題 | 不在本題 |
Question D3:
D3 — 意圖漏斗和「對談極速通道」都要改對談窗，誰先？
Project/branch/task: 意圖漏斗 3b（ConversationState 取代 WakeSession／fusion 窗）vs TODOS.md:7 新增的 P1 對談極速通道。
ELI10: 你剛把「對談極速通道」排成 P1，它的兩招（對談中跳過 cleaner、對談中把 VAD 縮到 300ms）都要先問「這個人現在是不是在對談窗裡」。可是意圖漏斗的 3b 正好要把對談窗整個搬家換實作。兩邊各寫各的，3b 就得多搬一個新使用者，或快速通道寫好沒多久就被 3b 打掉。延遲是大家真正有感的痛點，漏斗做到 3b 大約要 6 個 PR、一週左右，讓它乾等也不好。
Stakes if we pick wrong: 順序沒講好，3b 換窗口時快速通道會默默失效（對談中又變慢），或快速通道要等一週以上才開工。
Recommendation: C because 只多一個唯讀函式，兩條線都不用等，3b 換實作時只改一處，快速通道也不會讀到半搬家的狀態
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) 漏斗先做到 3b
  ✅ 快速通道直接接 ConversationState，不用搬兩次，介面最乾淨
  ✅ 3a-2 情境表不必涵蓋快速通道，漏斗範圍不變
  ❌ 延遲改善要等漏斗步驟 1 到 3b 都上線，約一週以上
B) 快速通道先做
  ✅ 延遲改善最快上線，直接打在使用者最有感的痛點
  ✅ 漏斗的情境表會把快速通道行為一起鎖住，搬家時有回歸保護
  ❌ 3b 要多搬一個窗口使用者，情境表與 3b 都變大
C) 並行，共用一個唯讀查詢 (recommended)
  ✅ 兩條線都不用等，延遲改善照 P1 節奏走
  ✅ 3b 換實作只改這個函式，快速通道不會默默失效
  ❌ 多一個要維持穩定的函式，且要保證從 VAD 執行緒讀也安全
Net: 介面最乾淨但延遲晚一週，還是延遲先上但漏斗變大，或多一個小接點讓兩邊並行。
Header: 先後順序
Options:
A) 漏斗先做到 3b
快速通道在 3b 合併後才開工，只讀 ConversationState 判斷對談窗；漏斗範圍不變（快速通道延後約 1 週以上，risk low）。
B) 快速通道先做
快速通道照 P1 先做，3a-2 情境表把「對談中跳 cleaner／VAD 縮短」當現行行為寫進去，3b 一起搬（3a-2＋3b 各多約 human ~半天 / CC ~20min，risk medium）。
C) 並行，共用一個唯讀查詢 (recommended)
在 WakeSessionMixin 加唯讀 `in_conversation_window(speaker, now) -> bool`（只讀現值、不改狀態、可從 VAD 執行緒呼叫，含跨執行緒測試）；快速通道只准用它判斷對談窗；3b 換實作時只改這個函式並保留其測試（human ~1h / CC ~10min，risk low）。

State: approved
Actual answer: C) 並行，共用一個唯讀查詢（使用者 2026-10-22，D3）
Accepted scope: 在 WakeSessionMixin 加唯讀 `in_conversation_window(speaker, now) -> bool`（只讀現值、不改狀態、可從 VAD 執行緒呼叫，含跨執行緒測試）；對談極速通道只准用它判斷對談窗；漏斗 3b 換實作時只改這個函式並保留其測試。這個函式誰先做誰加（漏斗或快速通道的第一個 PR）。快速通道本身的內容與延遲基準不在本題，需另做 eng review。
History: 無

### Section 2–4 發現（第 2 輪；皆為已核准範圍的必要實作，無新選擇）
- CQ2-1 [P3] (confidence: 6/10) cogs/voice_controller_wake_session.py:66-70 — Response Lock 是單一 bool，任何一個處理結束都會解鎖：A 被接受（上鎖）→ B 經對談窗覆蓋守衛被接受（再上鎖）→ A 處理完 finally 解鎖，B 還在處理時第三人可快速喚醒。1b790f9 之前「TTS 送達解鎖」也是同一行為，依 R7 保留現行；3a-2 情境表要收這一條（寫明是現行行為），RoomState 介面不得默默改成 per-turn 鎖。
- TEST2-1 [P2] (confidence: 9/10) 3a-2 測試改寫清單要加入第一輪 eng review 之後才出現的測試：tests/test_response_lock_release.py、tests/test_wake_session_barge_in.py、tests/test_barge_in_wiring.py、tests/test_tts_interrupt_device_path.py（皆在 origin/main）。
- TEST2-2 [P2] (confidence: 9/10) R12=A 的必要測試取代補充第 9 條：開窗設 presence、關窗清掉、被搶時直接改成新 target、presence 更新例外吞掉記 log、`WINDOWS=0` 不呼叫 presence、`active_text_channel` 為 None 不影響（presence 不依賴頻道）。
- TEST2-3 [P2] (confidence: 9/10) R13=C 的必要測試：`in_conversation_window` 在 target 窗內／窗外／他人／idle 逾時前後一刻（注入 now）；從非 loop 執行緒呼叫不拋例外且不改狀態（沿用 tests/test_barge_in_wiring.py 的跨執行緒範本）；3b 換實作後同一組測試對新實作全綠。
- PERF2-1：No issues found（presence 每天約 30–40 次更新；被搶連發時可能被 Discord 限流，已在失敗吞掉記 log 範圍內）。

### Section 2–4 Dispositions（第 2 輪）
- CQ2-1：accepted（R7 既有回歸契約的必要情境，不另議）
- TEST2-1：accepted（D11 測試改寫範圍的必要補列）
- TEST2-2：accepted（D2=A 的必要測試）
- TEST2-3：accepted（D3=C 的必要測試）

### Outside Voice（第 2 輪）
Outside voice unavailable: codex 未安裝；native Claude Plan 子代理因 API 429（session limit，18:20 重置）提前終止，未產出報告。Fix: 安裝 codex，或額度重置後重跑本審查的 Outside Voice。Continuing to planning decisions and Approval readiness.（OUTSIDE_VOICE_UNAVAILABLE）已以 status=unavailable 記入 review log。

### TODOS.md（第 2 輪）
- 既有 P1「對談模式極速低延遲通道」加註 D3=C 的接點限制與「動工前先跑 eng review」（TODOS.md:12）。無新增 TODO。

Approval readiness: PASS — R12（D2=A，使用者 2026-10-22）、R13（D3=C，使用者 2026-10-22）；SC2-*、CQ2-1、TEST2-1～3 為事實更正或既有核准（R7/D11、D2、D3）的必要實作。

### 第 2 輪對定稿的修訂摘要（實作以此覆蓋定稿對應條文）
- T0、T0.5 完成（SC2-1）；Response Lock 情境以「處理結束解鎖」為準（SC2-2、CQ2-1）。
- 3c 搬遷清單／flag 負責表補 3 個 Response Lock 讀寫點（SC2-3）。
- 3b 的 E2 改用 bot presence（D2=A）；CEO-D20 與補充第 9 條訊息／epoch 規則作廢。
- 新增唯讀接點 `in_conversation_window(speaker, now)`（D3=C），誰先做誰加；3b 只改它的實作。
- 3a-2 改寫清單補 4 個測試檔（TEST2-1）。

### NOT in scope（第 2 輪新增）
- 對談極速通道本身的設計與延遲基準：另做 eng review（D3）。
- Response Lock 改成 per-turn 鎖：不做，R7 保留現行（CQ2-1）。

### What already exists（第 2 輪新增）
- fab74a1 `_release_response_lock` 系列（步驟 0.5 已完成）；transcript_mirror.py（與 E2 衝突的原因）；tests/test_barge_in_wiring.py 跨執行緒範本（TEST2-3 沿用）。

### Failure modes（第 2 輪新增路徑）
| 路徑 | 實際失敗 | 測試／處理 | 使用者看到 |
|---|---|---|---|
| E2 presence 更新 | Discord 限流／例外 | 吞掉記 log＋TEST2-2 | 狀態暫時沒更新 |
| `in_conversation_window` 從 VAD 執行緒讀 | 讀到半更新狀態或在非 loop 建 task | 唯讀、不建 task＋TEST2-3 跨執行緒測試 | 無（最壞是該次不走快速通道） |
| 3b 換實作漏改接點 | 快速通道默默失效 | 同一組測試前後實作都要綠（TEST2-3） | 修好前對談又變慢 |
Critical gaps：0。

### Implementation Tasks（第 2 輪新增／修改）
- [x] **T0／T0.5** — 已完成（cc617b1、fab74a1 在 origin/main）。
- [ ] **T2.1 (P2, human: ~1h / CC: ~10min)** — WakeSessionMixin — 加唯讀 `in_conversation_window`
  - Surfaced by: Section 1 — R13（D3=C）
  - Files: cogs/voice_controller_wake_session.py、tests/
  - Verify: TEST2-3 情境＋跨執行緒測試；棘輪不升
- [ ] **T4 修改** — 3b 的 E2 改 presence（D2=A），驗收改 TEST2-2；不再需要訊息 id／epoch 測試。
- [ ] **T3a2 修改** — 改寫清單加 TEST2-1 四檔＋CQ2-1 情境。
- [ ] **T5 修改** — 搬遷清單加 SC2-3 三個讀寫點。
_No new tasks from Performance._

Worktree parallelization：依序實作（同定稿）；T2.1 可隨漏斗步驟 1 或快速通道第一個 PR 帶上。

### REALITY rows（第 2 輪）
- REALITY: premise-table finding unverified=1（presence 限流門檻未查證；其餘以 git/grep/棘輪量測核對） cogs/voice_controller_wake_session.py:66
- REALITY: already-done finding T0/T0.5 已在 origin/main（9a03386），標完成 cogs/voice_controller_wake_session.py:71
- REALITY: surface-check pass `MARVIN_TRANSCRIPT_MIRROR` 預設 "1"、`MARVIN_PER_SPEAKER_QUEUE` 預設 "0" 已核對 main_discord.py:350
- REALITY: binding-decisions finding D2=A 經使用者核准取代 CEO-D20；其餘 D4/D11/D14/D15/CEO-D21 未重議 docs/designs/intent-funnel-simplification.md:1725
- REALITY: numbers pass 棘輪 183/187、79/79、21/40 [measured: core_size_metrics] tests/test_voice_controller_size_budget.py:49
- REALITY: deferred-asks pass 無連結 issue docs/designs/intent-funnel-simplification.md:1
- REALITY: pr-count pass 本計畫 PR 數不變（T2.1 隨既有 PR 帶上，不另開） docs/designs/intent-funnel-simplification.md:1727

### Unresolved decisions（第 2 輪）
無。

### Completion summary（第 2 輪）
- Step 0: Scope Challenge — scope accepted as-is（結構沿用 D4）
- Reality rows: 7/7 applicable rows emitted, 1 unverified premise
- Architecture Review: 2 issues found（R12 E2 被鏡像洗掉、R13 與快速通道搶對談窗）
- Code Quality Review: 1 issue found（CQ2-1 Response Lock 共用 bool）
- Test Review: diagram 沿用第一輪＋第 2 輪測試計畫檔，3 gaps identified（TEST2-1～3）
- Performance Review: 0 issues found
- NOT in scope: written
- What already exists: written
- TODOS.md updates: 0 new items proposed（既有 P1 加註）
- Failure modes: 0 critical gaps flagged
- Unresolved decisions: 0 in this review
- Outside voice: codex 未安裝；native Claude 子代理 API 429 未完成 → unavailable
- Parallelization: 1 lane, 0 parallel / sequential（T2.1 可隨任一側 PR）
- Lake Score: N/A（D2、D3 為種類不同的選項，無完整度分數）

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 1 | CLEAR | 5 proposals, 3 accepted, 2 deferred |
| Outside Review | codex 未安裝 → native Claude Plan subagent | Independent 2nd opinion | 3 | unavailable（第 3 次 native 因 API 429 未完成） | eng 第 1 輪 14 條、CEO 12 條；eng 第 2 輪無報告 |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 2 | ISSUES OPEN | 6 issues, 0 critical gaps（全數已決：D2=A、D3=C，其餘為必要實作） |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **OUTSIDE COVERAGE:** codex，plan-review phase：三次皆 unavailable（CLI 未安裝）；native 子代理第 1、2 次完成（不計外部覆蓋），第 3 次（eng 第 2 輪）因 API 429 未完成。
- **VERDICT:** CEO CLEARED；Eng Review 第 2 輪 ISSUES OPEN（6 條皆已處置、0 未決，status 依規則為 issues_open）—— 可依定稿＋第 2 輪修訂動工步驟 1。

NO UNRESOLVED DECISIONS
