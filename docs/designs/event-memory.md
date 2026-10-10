# 事件記憶（Event Memory）— Phase 1：問了能答

狀態：設計草案（2026-10-10），待 plan-eng-review

## 目標

聊天中提到的生活事件（「明天要去露營」「下禮拜出國去日本」）結構化記下來，之後問得到：
「馬文我上個月去哪裡露營？」「狗與露上次出國去哪？」

Phase 2（本文件不做）：事件日期過後主動追問「上個月去 XX 露營好玩嗎？」，並**取代**現有 commitment→callback_queue 返場提醒。

## 使用者拍板（2026-10-10）

1. 保留 30 天（對齊 PRIVACY.md 摘要保留期，不改承諾）
2. 隱私規則 (b)：**在場者全部 ⊆ 該事件的 heard_by** 才能講（問答與日後主動提起同一條規則）
3. 事件追問取代 commitment callback——Phase 2 才動，Phase 1 不碰 callback_queue
4. 先做「問了能答」
5. **必須能整合**：同一事件討論 30 分鐘（跨 6 個 5 分鐘摘要窗）、STT 糊成不同寫法，最後要是 1 筆事件不是 3 筆

## 現況（素材已在）

- `session_summarizer.py` 每 5 分鐘一窗，LLM 摘要已寫到「showay 明天出國去日本」等事件，但只是自由文字
- `session_summaries.speakers` = 窗口內說話者 → 可當 heard_by
- `recall_handler.py` 路徑 C 只查 24h；`_RECALL_PATTERNS` regex 抓不到「上個月去哪露營」
- 摘要 30 天、transcripts 14 天保留（`scripts/prune_retention.py`）

## 設計

### 1. 資料表 `events`（marvin.db，新 `event_store.py`，仿 summary_store/task_store 慣例）

| 欄位 | 說明 |
|---|---|
| id | PK |
| guild_id | |
| owner | 事件主角（speaker 名） |
| activity | 活動，短詞（露營、出國、看醫生） |
| place | 地點原文，可 null（STT 可能糊，不用來對外講細節以外的推論） |
| event_date | 事件日期（台北時區當日 00:00 的 unix ts），可 null |
| date_text | 原文時間詞（明天、下禮拜六） |
| participants | JSON list，同行者（原文） |
| heard_by | JSON list，**所有被合併提及窗口 speakers 的聯集** |
| quotes | JSON list，最多 5 句原話（新的擠掉舊的） |
| first_seen / last_seen | 第一次 / 最後一次被提及的窗口結束時間 |
| created_at | |

### 2. 抽取：獨立 LLM 呼叫（不塞進摘要呼叫）

summarizer 存完摘要後，同一窗口再呼叫一次 `router._call_llm(tier="simple", purpose="extract_events")`。
理由：摘要呼叫是 8b 級 simple tier，再加事件+連結的 JSON 契約容易把摘要/承諾一起拖壞；拆開才符合「單一服務失敗不中斷」。
失敗 → log warning 跳過，不影響摘要。

Prompt 輸入：
- 該窗口逐字稿（同摘要）
- **近期事件清單**：同 guild、owner ∈ 窗口 speakers、last_seen 在 7 天內的事件，最多 10 筆，格式 `#id owner｜activity｜place｜date_text`
- 窗口時間（讓 LLM 把「明天」換算成日期 YYYY-MM-DD）

輸出：
```json
{"events":[{"same_as": 12 或 null, "owner":"...", "activity":"...", "place":"..."|null,
  "date":"YYYY-MM-DD"|null, "date_text":"...", "participants":[...], "quote":"原話一句"}]}
```
只收**現實生活、有時間指向的事件**（出遊、出國、看病、聚餐、搬家…）；遊戲內行動、閒聊抱怨、當下動作（去上廁所）不收——prompt 明列正反例。

### 3. 整合（兩層）

**A. 抽取時連結（主力）**：LLM 看到近期事件清單，判斷是同一件 → `same_as=id`。
30 分鐘討論的第 2~6 窗都會看到第 1 窗建的事件，糊字靠語意判斷。

**B. 本地確定性合併（兜底，零 LLM）**：寫入新事件前，在同 owner 的近期事件中找：
- event_date 相同或差 ≤1 天（雙方都 null 時改用 first_seen 差 ≤ 3 小時），**且**
- activity 拼音相似度 ≥ 門檻（用 `music_fastpath.to_pinyin` + rapidfuzz ratio；門檻在測試定死）

命中 → 併入既有事件。A 給的 `same_as` 若 id 不存在/不屬於同 owner → 忽略，退回 B。

合併規則：heard_by 取聯集、quotes append（上限 5）、participants 聯集、place/date 新值非 null 才覆蓋、last_seen 更新。

### 4. 查詢：`EventRecallAgent`（IntentBus，LLM-first）

- 新 DeclarativeIntentAgent，必填 `manifest_description`：「問自己或在場朋友過去/計畫中的生活行程（去哪玩、哪天出國、上次露營在哪）。不是查網路資訊、不是待辦清單」
- 少量 regex 當 fast path（上(個)?月|上次|之前|那次 … 去(哪|了哪)）；主要靠 regex miss→LLM tool calling 接
- 加進 `agent_gaps_resolved.json`
- Handler：
  1. 取 guild 近 30 天事件
  2. **隱私濾網**：在場者（`present_speakers(speaker, ctrl.get_online_members())`）全部 ⊆ heard_by 的才留
  3. 留下的事件（≤15 筆）+ 問題 → `_call_llm(tier="simple", purpose="event_recall")`，system prompt 規定**只能根據清單內容回答，清單沒有就說不記得**，不准補細節
  4. 無事件 → 固定句「我不記得有聽過這件事」，不呼叫 LLM

### 5. 保留

`prune_retention.py` 加 `prune_events`：last_seen < now-30d 刪除。PRIVACY.md 加一列「事件記憶 / marvin.db events / 30 天」。

## 不做（Phase 1）

- 主動追問、取代 callback_queue（Phase 2）
- 回填歷史資料進 prod 表（只做離線評估用）
- 「忘掉這件事」語音刪除（30 天自然過期；需要再加）

## 驗收標準

1. pytest：event_store CRUD、合併規則（B 層各邊界）、隱私濾網（在場者有人不在 heard_by → 該事件被濾掉）、same_as 非法 id 退回 B、LLM 失敗不影響摘要寫入、prune 30 天
2. **真實資料重播評估**（整合能力的真正驗收，不靠 mock）：離線腳本用近 14 天 transcripts 按 5 分鐘窗重跑抽取到暫存 DB，人工對照：
   - showay 10/3 出國日本：多窗提及 → 1 筆
   - 狗與露 10/3「明天一家人早出發」→ 1 筆
   - 遊戲內行動、上廁所類 → 0 筆
   - 產出事件總數與合併前後數字列表給使用者看
3. 實測：上線後問「馬文 showay 上次出國去哪」能答出日本；不在 heard_by 的人在場時答「不記得」

---

# Eng Review 紀錄（/plan-eng-review，2026-10-10）

審查目標：`docs/designs/event-memory.md`（本文件上半部，原始草案保留不動）

## Scope Challenge A：盤點

**現有可重用**
- SQLite store 慣例：`summary_store.py` / `task_store.py`（`db_path="marvin.db"`、`:memory:` 測試模式、`_connect/_release`）
- 在場名單：`speaker_isolation.present_speakers()`、`VoiceController.get_online_members()`（cogs/voice_controller.py:2236）
- 拼音：`music_fastpath.to_pinyin` + rapidfuzz（已安裝）
- IntentBus LLM-first：`DeclarativeIntentAgent` + `manifest_description`（intent_agents/base.py:132）
- 保留清理：`scripts/prune_retention.py`（每日 03:00）
- 同意閘：`handle_stt_result` 開頭擋未同意者（voice_controller.py:1023）→ transcripts 只有同意者 → 事件自動繼承，不需另加

**實測數據**
- `summarize_window` 近 7 天每天 15~174 次；模型 83% 是 `ministral-8b-latest`（records/llm_routing.jsonl）
- Prior learning applied: dj-fabrication-ministral（confidence 9/10, 2026-10-01）— ministral-8b 無視「不准編造」規則
- voice_controller.py statement 2079/2200；新 IntentAgent 接線 = 0 statement

**複雜度**：約 10 檔（新 2 模組 + summarizer + VC 一行 + prune + PRIVACY.md + agent_gaps_resolved.json + main_discord 日誌白名單 + 測試 + 離線重播腳本），新類別 2 個（EventStore、EventRecallAgent）→ 觸發複雜度關卡。

**Search check**：無新基礎設施/併發模式（SQLite + 既有 LLM bus + 既有 IntentBus），不做外部搜尋，以既有 repo 慣例為準。

## Scope Challenge B：範圍紀錄

feature answers: 無提議刪減（使用者已拍板範圍 1~5）；structure: B Smaller arrangement（D1, 2026-10-10）；accepted scope: 新 `event_memory.py`（EventStore + 抽取 prompt + 解析 + 合併純函式）+ 新 `intent_agents/event_recall_agent.py`；`session_summarizer.py` 只加一行 try/except hook；其餘照原稿；pending remedies: none

## Scope Challenge C

No issues found（範圍問題）。Scope Challenge result: scope accepted as-is（結構改小版不算刪減）→ MODE = FULL_REVIEW

## Section 1：Architecture

`[P1] (confidence: 9/10) session_summarizer.py:120 — heard_by 只取窗口「有說話的人」，在場但沒開口的人不算聽到`
引用：`speakers = list({u["speaker"] for u in utterances})`；設計 §1「heard_by = 所有被合併提及窗口 speakers 的聯集」。
後果：大肚全程在場聽 showay 講日本行程但沒插話 → 之後大肚在場時問「showay 上次出國去哪」，隱私規則 (b) 判定大肚沒聽過 → 拒答。

`[P1] (confidence: 9/10) docs/designs/event-memory.md §4.3 — 回答交給 tier="simple" LLM 自由生成，實際 83% 落在 ministral-8b`
引用：設計「留下的事件 + 問題 → _call_llm(tier="simple", purpose="event_recall")，system prompt 規定只能根據清單內容回答」；records/llm_routing.jsonl 近 7 天 summarize_window 546/653 次是 ministral-8b-latest。
Prior learning applied: dj-fabrication-ministral（confidence 9/10, from 2026-10-01）— 同模型曾無視不准編造規則。使用者鐵則「事實類素材別讓 LLM 自由發揮，說錯不如沒說」。

`[P2] (confidence: 8/10) docs/designs/event-memory.md §2 — 抽取+連結(same_as) 也走 tier="simple"，整合能力的主力判斷落在 ministral-8b`
引用：設計「router._call_llm(tier="simple", purpose="extract_events")」；gemini_router_llm.py:372 `_call_llm(..., prefer_providers: tuple = ())` 已支援指定 provider 優先；gemini_router_content.py:21 `DJ_PREFER_PROVIDERS = ("gemini_free", "groq")` 是現成先例。
近 3 天 gemini-3.1-flash-lite 661 次、ministral 785 次；抽取每天約 15~174 次會加到 gemini_free 免費額度上。

## Decision ledger

### R1: heard_by 的來源
Finding: Arch #1, P1, 9/10, session_summarizer.py:120, reviewer=claude
Plan baseline: 原稿 heard_by = 被合併窗口的 speakers 聯集
Runtime evidence: transcripts 只有說話者；get_online_members()（voice_controller.py:2236）回傳當下語音頻道全部真人
Comparison grid:
| Choice | Current | A | B |
|---|---|---|---|
| R1 heard_by 來源 | 窗口 speakers | speakers ∪ 窗口結束時在頻道的真人 | 維持只有 speakers |
| R2 回答生成 | pending | pending | pending |
| R3 抽取模型 | pending | pending | pending |
Question D2: heard_by 要不要算進在場但沒說話的人（A 推薦）
Header: heard_by來源
Options:
A) 說話者加在場者
summarizer 建構時多收一個 members 函式（VC 傳 get_online_members），窗口結束時 speakers ∪ 在場真人當 heard_by。人工 1h / CC 5 分。
B) 只算說話者
維持原稿，較嚴格；沉默旁聽者在場時會被拒答。

State: approved
Actual answer: A) 說話者加在場者（D2, 2026-10-10）
Accepted scope: SessionSummarizer 建構多收 members_fn（VC 傳 get_online_members）；heard_by = 窗口 speakers ∪ 窗口結束時在頻道真人；合併時取聯集；附測試
History: —

### R2: 問答回覆怎麼產生
Finding: Arch #2, P1, 9/10, design §4.3, reviewer=claude
Plan baseline: 原稿 LLM 讀事件清單自由寫答案
Runtime evidence: ministral-8b 佔 simple tier 83%，曾編造
Comparison grid:
| Choice | Current | A | B | C |
|---|---|---|---|---|
| R1 heard_by | 依 D2 | 不變 | 不變 | 不變 |
| R2 回答生成 | LLM 自由寫 | LLM 只回事件 id（JSON），本地模板組句＋原話 | LLM 自由寫但 prefer gemini_free/groq | 維持原稿 |
| R3 抽取模型 | pending | pending | pending | pending |
Question D3: 回答由本地模板組句、LLM 只挑事件（A 推薦）
Header: 回答生成
Options:
A) LLM只挑id模板組句
LLM 回 {"ids":[..]}，非清單內 id 丟棄；本地模板：「你 10/4 說要去日本，那時說『…』」。不可能編造地點日期。人工 2h / CC 10 分。
B) 換模型照樣自由寫
prefer_providers=("gemini_free","groq")，仍可能潤飾出不存在的細節。人工 30 分 / CC 3 分。
C) 維持原稿

State: approved
Actual answer: A) LLM只挑id模板組句（D3, 2026-10-10）
Accepted scope: event_recall LLM 只輸出 {"ids":[...]}（is_json=True）；不在候選清單內的 id 丟棄；回覆由本地模板從 DB 欄位＋一句原話組成；無命中→固定句；附測試（含 LLM 回非法 id、回壞 JSON、逾時）
History: —

### R3: 抽取+連結用哪個模型
Finding: Arch #3, P2, 8/10, design §2, reviewer=claude
Plan baseline: tier="simple"（實際多半 ministral-8b）
Runtime evidence: prefer_providers 已存在、DJ 先例 ("gemini_free","groq")
Comparison grid:
| Choice | Current | A | B |
|---|---|---|---|
| R3 抽取模型 | simple 無偏好 | simple + prefer ("gemini_free","groq") | 維持 |
Question D4: 抽取呼叫優先用 gemini_free/groq（A 推薦）
Header: 抽取模型
Options:
A) 偏好gemini/groq
跟 DJ 同設定；免費額度多用每天最多約 170 次。重播評估時 log 實際模型。人工 15 分 / CC 2 分。
B) 維持simple
不動額度，合併判斷交給 ministral，靠 B 層拼音兜底。

State: approved
Actual answer: A) 偏好gemini/groq（D4, 2026-10-10）
Accepted scope: extract_events 呼叫 tier="simple" + prefer_providers=("gemini_free","groq")（引用 gemini_router_content.DJ_PREFER_PROVIDERS 或同值常數）；重播評估腳本記錄每窗實際模型
History: —

Section 1 Dispositions: Arch#1 accepted（D2-A）；Arch#2 accepted（D3-A）；Arch#3 accepted（D4-A）

## Section 2：Code Quality

`[P2] (confidence: 10/10) main_discord.py setup_early_logging — 新頂層模組 event_memory.py 的 INFO log 會被 root WARNING 吞掉`
Prior learning applied: new-toplevel-logger-needs-info-allowlist（confidence 10/10, from 2026-09-25）。D1 選 B 時已列為該選項的必要代價 → 屬已核准結構的必要實作，不另問。

`[P2] (confidence: 8/10) docs/designs/event-memory.md §2 — LLM 輸出欄位沒有本地驗證就入庫`
引用設計輸出格式 `"owner":"...", "date":"YYYY-MM-DD"|null`。實際風險：LLM 把「明天」算錯年份/月份、owner 填出不在場的名字（或「馬文」）、activity 空字串 → 垃圾事件；現有 callback_queue 已經收進「去上廁所」「查詢什麼是生綠塔」（suki_memory.json 馬文條目），證明這條抽取線會漏垃圾。

### R4: LLM 事件欄位入庫前驗證
Finding: CQ #2, P2, 8/10, design §2, reviewer=claude
Plan baseline: 原稿只靠 prompt 正反例，無本地驗證
Runtime evidence: callback_queue 現有雜訊（馬文: 查詢什麼是生綠塔；狗與露: 去上廁所）
Comparison grid:
| Choice | Current | A | B |
|---|---|---|---|
| R4 入庫驗證 | 無 | owner ∈ 該窗 heard_by 且 ≠ 馬文/Marvin；activity 非空且 ≤12 字；date 可解析且落在 [窗口日-60 天, 窗口日+365 天]，否則 date 設 null（事件仍收）；quote 必須是該窗逐字稿子字串，否則改用 activity 當 quote 不存原話 | 不驗證 |
Question D5: LLM 事件欄位入庫前做本地驗證（A 推薦）
Header: 入庫驗證
Options:
A) 本地驗證
owner/activity 不合格整筆丟；date 不合理只清成 null；quote 非逐字稿子字串就不存。人工 2h / CC 10 分（含表格式測試）。
B) 不驗證
信任 prompt；垃圾事件進庫，問答時可能被選中。

State: approved
Actual answer: A) 本地驗證（D5, 2026-10-10）
Accepted scope: 純函式 validate_event()：owner ∈ 該窗 heard_by 且非 馬文/Marvin、activity 非空且 ≤12 字，否則整筆丟；date 不可解析或不在 [窗口日-60天, 窗口日+365天] → null；quote 去標點空白後不是逐字稿（同樣正規化）子字串 → 不存；表格式測試
History: —

### R5: place（地點）是否也要求出現在逐字稿
Finding: CQ #2 延伸, P2, 7/10, design §1 place 欄位, reviewer=claude
Plan baseline: place 為 LLM 原文，無驗證（D5 只涵蓋 owner/activity/date/quote）
Runtime evidence: 摘要曾把「中橫」寫成「中基站」（session_summaries 2026-09-30），LLM 也可能把「去沖繩」改寫成「日本沖繩」
Comparison grid:
| Choice | Current | A | B |
|---|---|---|---|
| R4 入庫驗證 | approved D5-A | 不變 | 不變 |
| R5 place 驗證 | 無 | 去標點空白後須為該窗逐字稿子字串，否則 null | 不驗證，place 可為 LLM 改寫 |
Question D6: 地點也要求逐字稿真的出現過（A 推薦）
Header: 地點驗證
Options:
A) 地點須在逐字稿
不合格只清成 null，事件保留；模板沒地點就不講地點。人工 30 分 / CC 3 分。
B) 地點不驗證
保留 LLM 正規化後的地名，可能是推論或改寫。

State: approved
Actual answer: A) 地點須在逐字稿（D6, 2026-10-10）
Accepted scope: validate_event() 加 place 檢查：去標點空白後非該窗逐字稿子字串 → place=null（事件保留）；模板 place 為 null 時不講地點；表格式測試列
History: —

Section 2 Dispositions: CQ#1 accepted（D1-B 必要代價）；CQ#2 accepted（D5-A + D6-A）

## Section 3：Test review

框架：pytest（pytest.ini、`@pytest.mark.asyncio`；不用 asyncio.run）。測試由 Claude 自己跑 `./venv_simon/bin/python -m pytest`。

```
CODE PATHS                                                   USER FLOWS
[+] event_memory.py（新）                                     [+] 「馬文 我上個月去哪露營」
  ├── EventStore                                              │   ├── [GAP] regex fast path 命中 → 答出活動/日期/原話
  │   ├── [GAP] add / get_recent(owner,7d) / list_guild(30d)  │   ├── [GAP] regex miss → manifest_description 讓 LLM
  │   ├── [GAP] merge_into(id, ...) 聯集/上限5/null不覆蓋     │   │        tool calling 選到 event_recall [→EVAL 重播]
  │   └── [GAP] prune(30d)                                     │   ├── [GAP] 在場有人不在 heard_by → 固定句「不記得」
  ├── parse_events(content)                                    │   └── [GAP] 沒任何事件 → 固定句，不呼叫 LLM
  │   ├── [GAP] 合法 JSON / ```json 包裹 / 壞 JSON → []         [+] 30 分鐘討論同一事件（6 窗）
  ├── validate_event(ev, heard_by, transcript, window_day)     │   └── [GAP] [→EVAL] 真實資料重播：showay 日本 → 1 筆
  │   ├── [GAP] owner 不在 heard_by / 是馬文 → 丟             [+] 錯誤狀態
  │   ├── [GAP] activity 空 / >12 字 → 丟                      │   ├── [GAP] 抽取 LLM 失敗/逾時 → 摘要照存（CRITICAL）
  │   ├── [GAP] date 壞格式 / 超出 [-60,+365] 天 → null       │   └── [GAP] recall LLM 逾時/壞 JSON/非法 id → 不記得
  │   ├── [GAP] quote 非逐字稿子字串（正規化）→ 不存
  │   └── [GAP] place 非逐字稿子字串 → null
  ├── find_merge_target(ev, recent)
  │   ├── [GAP] same_as 合法（同 owner、存在）→ 該 id
  │   ├── [GAP] same_as 不存在 / 別人的 → 退回拼音規則
  │   ├── [GAP] date 相同或差 ≤1 天 + 拼音 ≥ 門檻 → 合併
  │   ├── [GAP] 雙方 date null + first_seen 差 ≤3h + 拼音 → 合併
  │   └── [GAP] 不同 owner / 拼音低於門檻 → 新事件
  └── extract_and_store(router, ...)
      └── [GAP] prefer_providers=("gemini_free","groq"), purpose="extract_events"
[+] session_summarizer.py（改）
  ├── [★★★ TESTED] 摘要/commitments 既有 13 條（tests/test_session_summarizer.py）
  ├── [GAP] event_memory=None → 不呼叫第二次 LLM（既有測試不變）
  ├── [GAP] heard_by = speakers ∪ members_fn()；members_fn 丟例外 → 只用 speakers
  └── [GAP] 抽取丟例外 → save_summary 已完成、commitments 照送（CRITICAL 回歸）
[+] intent_agents/event_recall_agent.py（新）
  ├── [GAP] manifest_description 非空（test_intent_manifest 既有機制）
  ├── [GAP] 可見性濾網 present ⊆ heard_by
  ├── [GAP] LLM 回 ids ∩ 候選；非候選 id 丟
  └── [GAP] 模板只用 DB 欄位；place null 不講地點
[+] scripts/prune_retention.py（改）  └── [GAP] events last_seen < now-30d 刪、dry-run 不刪
[+] main_discord.py（改）              └── [GAP] event_memory logger INFO 不被吞

COVERAGE: 1/30 paths tested（既有摘要）| GAPS: 29（2 個 →EVAL）
```

**CRITICAL 回歸契約**（已由 D1-B「summarizer 只加 try/except hook、失敗不影響摘要」核准，屬必要證明）：
既有 `tests/test_session_summarizer.py` 13 條不改即全綠；新增「抽取丟例外時 summary 已存、commitments callback 照送」。

**LLM/eval**：抽取 prompt 與 recall 選 id prompt 都是新 prompt → [→EVAL] = `scripts/replay_event_extraction.py` 真實資料重播（設計 §驗收 2），不用手寫 mock 當 baseline（使用者鐵則：mock 測產品不用自己當 fixture）。

**Tests to retire**：none。

Test plan artifact：見 ~/.gstack/projects/<slug>/ 下 eng-review-test-plan。

Section 3 Dispositions：所有 GAP 列為已核准行為的必要證明（D1~D6），無新增可選測試深度問題。

## Section 4：Performance

`[P2] (confidence: 8/10) summary_store.py:45 save_summary / 設計 EventStore — 在 async summarize_window 裡同步開 SQLite`
引用：session_summarizer.py `self.summary_store.save_summary(` 直接在 coroutine 內呼叫；transcripts 寫入則用 `asyncio.to_thread(self._transcript_store.save, ...)`（voice_controller.py:1204）。sqlite3.connect 預設 timeout 5 秒，marvin.db 有多個寫入者，鎖等待會卡住 event loop（語音收發同一個 loop）。EventStore 每窗要讀近期事件 + 寫/合併，每天 15~174 窗。
規模：每窗 1 讀 + ≤N 寫，資料列 < 千筆；正常 < 5ms，鎖競爭時最壞 5s 阻塞。

### R6: EventStore 呼叫是否丟到 thread
Finding: Perf #1, P2, 8/10, session_summarizer.py save_summary 呼叫點, reviewer=claude
Plan baseline: 原稿未指定（照 summary_store 慣例 = 同步）
Runtime evidence: transcript 寫入已用 to_thread；summary 寫入同步
Comparison grid:
| Choice | Current | A | B |
|---|---|---|---|
| R6 EventStore I/O | 同步（未指定） | extract_and_store 與 recall 的 DB 讀寫一律 asyncio.to_thread | 同步，照 summary_store |
| summary_store 既有同步寫入 | 同步 | 不動（不順手重構） | 不動 |
Question D7: 事件 DB 讀寫丟 thread、不卡語音 loop（A 推薦）
Header: DB執行緒
Options:
A) to_thread
新程式碼的 SQLite 操作都包 asyncio.to_thread；既有 summary_store 不動。人工 30 分 / CC 3 分。
B) 同步
跟 summary_store 一致；鎖競爭時最壞卡 loop 5 秒。

State: approved
Actual answer: A) to_thread（D7, 2026-10-10）
Accepted scope: event_memory.extract_and_store 與 EventRecallAgent 的 SQLite 讀寫一律 asyncio.to_thread；summary_store 不動
History: —

Section 4 Dispositions: Perf#1 accepted（D7-A）

## Outside Voice

Codex CLI 未安裝（CODEX_MODE: not_installed）；原生替代需要的 TaskOutput 工具不在本 session → outside voice unavailable，無第二意見覆蓋。

## TODOS.md

- D8 → A：已加入「事件記憶 Phase 2 — 事件過後主動追問，取代 commitment callback」
- D9 → A：已加入「事件記憶 — 『馬文忘掉這件事』語音撤回」

Approval readiness: PASS（R1=D2-A, R2=D3-A, R3=D4-A, R4=D5-A, R5=D6-A, R6=D7-A；結構 D1-B；TODO D8-A, D9-A）

## 定案設計（審查後，取代上半部有衝突之處）

```
每 5 分鐘 SessionSummarizer.summarize_window
  ├─ LLM 摘要 → save_summary（不變）→ commitments callback（不變）
  └─ if event_memory: try:
        heard_by = speakers ∪ members_fn()        （members_fn 失敗 → speakers）   [D2]
        recent  = to_thread(store.recent(owner∈heard_by, 7d, ≤10))                [D7]
        content = _call_llm(EXTRACT_PROMPT, transcript+recent+窗口日期星期,
                            tier="simple", is_json=True, purpose="extract_events",
                            prefer_providers=("gemini_free","groq"))              [D4]
        for ev in parse_events(content):
            ev = validate_event(ev, heard_by, transcript, window_day)            [D5][D6]
            if ev is None: continue
            target = find_merge_target(ev, recent)   # same_as 合法 → 該 id；否則拼音+日期規則
            to_thread(store.merge(target, ev) if target else store.add(ev))
     except Exception: log warning（摘要已存，不受影響）

「馬文 showay 上次出國去哪」→ IntentBus（regex fast path / manifest LLM tool calling）
  └─ EventRecallAgent handler
       events  = to_thread(store.list_guild(30d))                                 [D7]
       visible = [e for e in events if set(present) ⊆ set(e.heard_by)]           （規則 b）
       if not visible → 「我不記得有聽過這件事」（不呼叫 LLM）
       ids = _call_llm(SELECT_PROMPT, 問句+visible 清單, is_json=True, purpose="event_recall")
       picked = [e for e in visible if e.id in ids]                               [D3]
       if not picked → 固定句
       TTS(render_template(picked[0]))   # 只用 owner/activity/place/event_date/quote 欄位
```

## NOT in scope

- Phase 2 主動追問與拔 callback_queue：使用者定先做問答（已入 TODOS.md）
- 語音撤回事件：D9 延後（已入 TODOS.md）
- 歷史資料回填進 prod events 表：只做離線重播評估，避免把舊雜訊灌進庫
- summary_store 既有同步 SQLite 改 to_thread：不順手重構（D7 明確排除）
- recall 選 id 呼叫的 provider 偏好：沒列入決策，維持 tier 預設；選錯只會答錯「哪一筆」，不會編造內容

## What already exists

- 摘要窗口與 LLM 呼叫：`session_summarizer.py`（重用其窗口與逐字稿，不另開 loop）
- SQLite store 慣例：`summary_store.py`、`task_store.py`（重用模式，新寫 EventStore）
- 在場名單：`VoiceController.get_online_members()`（重用，D2）
- LLM provider 偏好：`gemini_router_content.DJ_PREFER_PROVIDERS`（重用值，D4）
- 拼音：`music_fastpath.to_pinyin` + rapidfuzz（重用於合併兜底）
- IntentBus manifest：`DeclarativeIntentAgent.manifest_description`（重用）
- 保留清理：`scripts/prune_retention.py`（擴充）
- 同意閘：`handle_stt_result` 上游已擋（不需新增）
- 不重用：`RecallHandler` 路徑 C（24h 摘要自由生成，與 D3 模板原則不同；保持不動）

## Failure modes

| 路徑 | 實際失敗 | 測試 | 錯誤處理 | 使用者看到 |
|---|---|---|---|---|
| 抽取 LLM | 全 provider 失敗/逾時 | 有（CRITICAL 回歸） | try/except + warning | 無感，該窗沒事件 |
| 抽取輸出 | 壞 JSON / 欄位亂填 | 有（parse/validate 表格） | 丟棄或清 null | 無感 |
| 連結 | same_as 指向別人的事件 | 有 | 退回拼音規則 | 無感 |
| 合併兜底 | 拼音門檻過寬把兩件事併一起 | 有（門檻邊界）+ 重播評估 | 無 | 答出另一件事的日期 → 重播評估把關 |
| members_fn | 不在語音頻道/例外 | 有 | 退回 speakers | 無感（較嚴格） |
| DB 鎖 | marvin.db 鎖等待 | 無（to_thread 隔離） | to_thread | 該窗延遲寫入，語音不卡 |
| recall LLM | 逾時/壞 JSON/非法 id | 有 | 固定句 | 「我不記得有聽過這件事」 |
| 意圖路由 | regex miss 且 LLM 沒選到 event_recall | 重播評估 | 掉回一般聊天 | 一般聊天回答（可能被 grounded 搶走）→ 上線實測項 |

Critical gaps：0

## Worktree parallelization

| Step | Modules touched | Depends on |
|---|---|---|
| S1 event_memory 核心 + 測試 | 頂層新模組、tests/ | — |
| S2 summarizer hook + VC 接線 | session_summarizer、cogs/ | S1 |
| S3 EventRecallAgent + 註冊 | intent_agents/、cogs/ | S1 |
| S4 prune + PRIVACY + 日誌白名單 + agent_gaps_resolved | scripts/、根目錄設定 | S1 |
| S5 重播評估腳本 | scripts/ | S1 |

Lane A: S1 → S2 → S3（S2/S3 都碰 cogs/voice_controller.py，序列化）。Lane B: S4、S5 在 S1 合併後可並行。
實務建議：規模小，單一 sonnet 任務一次做完 S1~S5，在獨立 worktree 跑（記憶：主目錄＝bot 執行目錄）。

## Implementation Tasks

- [ ] **T1 (P1, human: ~1d / CC: ~20min)** — event_memory.py — 新增 EventStore（events 表＋index (guild_id, owner, last_seen)）、parse_events、validate_event、find_merge_target、merge 規則、extract_and_store
  - Surfaced by: Scope B 結構 D1；Arch D2/D4；CQ D5/D6；Perf D7
  - Files: event_memory.py, tests/test_event_memory.py
  - Verify: `./venv_simon/bin/python -m pytest tests/test_event_memory.py -q`
- [ ] **T2 (P1, human: ~2h / CC: ~10min)** — session_summarizer — 加 event_memory=None、members_fn=None 參數與 try/except hook；VC 建構時傳入
  - Surfaced by: D1、D2；CRITICAL 回歸契約
  - Files: session_summarizer.py, cogs/voice_controller.py, tests/test_session_summarizer.py
  - Verify: 既有 13 條不改即綠 + 新增抽取失敗不影響摘要
- [ ] **T3 (P1, human: ~3h / CC: ~15min)** — EventRecallAgent — manifest_description、regex fast path、可見性濾網、LLM 只回 ids、模板組句、固定句
  - Surfaced by: Arch D3
  - Files: intent_agents/event_recall_agent.py, cogs/voice_controller.py（agent list 一元素）, agent_gaps_resolved.json, tests/test_event_recall_agent.py
  - Verify: pytest + `tests/test_voice_controller_size_budget.py` 綠
- [ ] **T4 (P2, human: ~1h / CC: ~5min)** — 保留與合規 — prune_events(30d)、PRIVACY.md 新增一列、main_discord 日誌 INFO 白名單
  - Surfaced by: 設計 §5；CQ#1 prior learning
  - Files: scripts/prune_retention.py, PRIVACY.md, main_discord.py, tests/test_prune_retention.py, tests/test_main_discord_logging.py
  - Verify: pytest 對應檔
- [ ] **T5 (P1, human: ~3h / CC: ~15min)** — 重播評估 — scripts/replay_event_extraction.py：近 14 天 transcripts 按 5 分鐘窗重跑抽取到暫存 DB（不寫 prod），輸出事件清單＋每窗實際模型＋合併前後數字
  - Surfaced by: Test review [→EVAL]；驗收 2
  - Files: scripts/replay_event_extraction.py
  - Verify: 人工對照 showay 10/3 日本 → 1 筆、狗與露 10/3 出發 → 1 筆、遊戲/上廁所 → 0 筆

## Unresolved decisions

無。

## Completion summary

- Step 0: Scope Challenge — scope accepted as-is（結構改小版 D1）
- Architecture Review: 3 issues found
- Code Quality Review: 2 issues found
- Test Review: diagram produced, 29 gaps identified（全為新程式碼路徑，含 2 個 →EVAL）
- Performance Review: 1 issue found
- NOT in scope: written
- What already exists: written
- TODOS.md updates: 2 items proposed to user（2 accepted）
- Failure modes: 0 critical gaps flagged
- Unresolved decisions: 0 in this review
- Outside voice: codex, unavailable（CLI 未安裝，原生替代工具不可用）
- Parallelization: 2 lanes, 1 parallel / 1 sequential
- Lake Score: 6/6 = D2~D7 六題皆選最高完整度選項

## Suppressed findings

無。

## 實作後修正（2026-10-10 重播 10/3 真實資料）

- 回答不念地點（使用者選 a）：重播把「我還在臺中，明天早上走」存成 place=臺中（出發地被當目的地）。D6 只保證地名出現在逐字稿，保證不了語意。place 仍存著，只用於選事件。
- 抽取模型：重播時 prefer gemini_free/groq 仍有 43/73 落到 ministral-8b（連續重播把兩家 TPM 打滿）。使用者決定先上線觀察 records/llm_routing.jsonl 的 extract_events 模型分佈，不加排除名單。
- 登記 purpose：extract_events / event_recall 加進 llm_agents/base.py 已知 purpose。
- 同人同日同地也合併（缺口 1，重播 9/29「去臺東」vs「回老家（臺東）」）。
- 從聊天學老家（缺口 2，使用者選「從聊天自己學」）：同一句同時有老家/家鄉/故鄉與地名才學；抽事件時附背景；「回老家」的 place 可用已知老家通過驗證。hometowns 表保留 30 天。

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 2 | — | 非本計畫 |
| Outside Review | codex（plan-review） | Independent 2nd opinion | 23 | unavailable | CLI 未安裝 |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 25 | issues_open | 35 issues（6 findings + 29 test gaps，全部已核准處置）, 0 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | 無 UI 範圍 |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **OUTSIDE COVERAGE:** codex, plan-review, unavailable, 0 findings（缺第二意見覆蓋）
- **VERDICT:** Eng Review ISSUES OPEN（問題皆已決策並轉成 T1~T5 實作任務）— eng review required 狀態以實作完成後 /review 收尾

NO UNRESOLVED DECISIONS
