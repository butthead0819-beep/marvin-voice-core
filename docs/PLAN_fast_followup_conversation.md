# 計劃書：對談模式極速低延遲通道（追上小智 AI 延遲指標）

> 建立日期：2026-10-20  
> 核心目標：喊了「馬文」或進入連續對談視窗（Follow-up）後，將端到端語音回應延遲從目前的 **1.6s～2.8s** 壓到 **600ms～800ms**（極限對標小智 AI 體感）。  
> 接手前必讀：[`AGENTS.md`](file:///Users/jackhuang/Code/Discord-voice-bot/AGENTS.md)、[`CLAUDE.md`](file:///Users/jackhuang/Code/Discord-voice-bot/CLAUDE.md)、[`ARCHITECTURE.md`](file:///Users/jackhuang/Code/Discord-voice-bot/ARCHITECTURE.md)。

---

## 0. 鐵則（Constitutional Rules）

1. **TDD 強制**：每個 PR 必須先在 `tests/` 建立失敗測試，確認紅燈後最小實作，確認綠燈同 commit 提交。
2. **LLM 一律走 Bus / 指定串流通道**：不准在 controller 內自開 client 或寫死 model ID。聊天串流維持 `router.stream_fast_response()`，非串流維持 `router._call_llm()`。
3. **不能破壞既有音樂與控制 FastPath**：[`music_fastpath.py`](file:///Users/jackhuang/Code/Discord-voice-bot/music_fastpath.py) 與 [`wake_shortcut.py`](file:///Users/jackhuang/Code/Discord-voice-bot/wake_shortcut.py) 依舊為最高優先級（T0）。
4. **記憶體安全（M1 8GB）**：不引入重量級本機端到端語音模型，純依賴架構管線剪枝（Pruning）與流式重疊（Streaming Overlap）。

---

## 1. 現狀分析與延遲預算拆解

### 1.1 為什麼現在 Marvin 對談體感比小智慢？

實測與日誌打點分析，目前 Marvin 從使用者「閉嘴（語音結束）」到「Discord 喇叭出聲」需等待 **1.6s ～ 2.8s**。拆解如下：

```
[使用者說話完畢]
   │
   ├── 1. VAD 靜音裁決 (Silero/WebRTC VAD) ─────── 400ms ~ 600ms （等靜音判定講完）
   │
   ├── 2. 音訊寫入 WAV + Swift STT (整段轉錄) ─── 350ms ~ 550ms
   │
   ├── 3. _confirmation_flow + clean_for_worker ── 300ms ~ 600ms ⚠️【巨大痛點：額外打一次 LLM】
   │
   ├── 4. IntentBus bid 競標 ───────────────────── ≤5ms
   │
   ├── 5. stream_fast_response (等待第一個整句標點)─ 350ms ~ 600ms ⚠️【等待 token 數太多】
   │
   └── 6. edge-tts 合成整句並送播放 ────────────── 350ms ~ 550ms ⚠️【首段音訊過大】
   │
[Discord 喇叭出聲] ── 累計延遲：1755ms ~ 2905ms
```

### 1.2 小智 AI 的 700ms 延遲預算表（目標標準）

| 階段 | 小智 AI (xiaozhi-esp32) | Marvin 現狀 | 本計劃優化後目標 (Marvin FastPath) |
| :--- | :--- | :--- | :--- |
| **VAD 結句** | ~300ms | 450ms | **320ms**（對話中動態縮短 silence） |
| **STT 轉錄** | 150ms（邊講邊串流） | 400ms（整段轉錄） | **250ms**（維持本機 Swift/Groq，精簡音訊長度） |
| **清洗與意圖** | **0ms**（直接進主模型） | 450ms（cleaner LLM）| **0ms（對談模式直接 Bypass Cleaner）** ⚡ |
| **Intent 分流** | 0ms（Function Calling） | 5ms（IntentBus） | **5ms**（維持極速同步 bid） |
| **LLM 首字/首片** | 200ms（首標點立即出） | 400ms（等待整句句號）| **180ms（前綴逗號切片，4~7 字即出）** ⚡ |
| **TTS 首段播放** | 150ms（微切片邊做邊播） | 400ms（整句生成送播）| **200ms（首句短片段優先 edge-tts）** ⚡ |
| **總端到端延遲**| **~800ms** | **~2100ms** | **~755ms（追平小智）** 🚀 |

---

## 2. 核心架構改造設計

### 改造 A：對談模式下 Cleaner Bypass（零中介延遲）

* **現狀**：[`cogs/voice_controller.py:2589`](file:///Users/jackhuang/Code/Discord-voice-bot/cogs/voice_controller.py#L2589) 在 `_confirmation_flow` 內呼叫 `clean_for_worker`（[`etd_clean_reuse.py`](file:///Users/jackhuang/Code/Discord-voice-bot/etd_clean_reuse.py)），這會打 `clean_stt_text` 做 LLM 清洗。
* **變更邏輯**：
  * 當滿足以下條件之一，判定處於**「確定對談中（Active Dialogue Context）」**：
    1. 剛喚醒（`is_fast=True`，且已明確命中喚醒詞）。
    2. 處於連續對話視窗（`_wake_session_captured(speaker) == True` 或 `self._pending_followups` 命中）。
  * 在「確定對談中」，**完全跳過 `clean_for_worker`**，直接將 `stripped` 視為最終 query（直接標記 `pipeline_timing.mark("cleaner_done")`）。
  * 節省時間：**300ms ～ 600ms**。

### 改造 B：指令 vs 問答「雙軌 5ms 極速分流」

* **現狀**：部分短指令（如「下一首」、「大聲一點」）若未被 `_detect_music_command` 捕捉，會因為長度 `< 4` 掉進 `_confirmation_flow` 的 `await asyncio.wait_for(evt.wait(), timeout=...)` 枯等後續音訊！
* **變更邏輯**：
  * 在進入 `_confirmation_flow` 前，**先執行 IntentBus 的預出價（Pre-Bid）**。
  * 若有任何高優先權 IntentAgent（如 `PlaybackControlAgent`、`VolumeAgent`、`MusicSearchAgent`）給出 `confidence >= 0.85`：
    * **立即短路退出 confirmation 等待**，直接 dispatch 給該 IntentAgent 執行！
    * 使用預先快取好的短語音（如「好的」、「切換中」）於 200ms 內出聲回饋。
  * 若無高信心 IntentAgent 認領，且 `len(stripped) >= 2`，直接推定為「閒聊問答」，**不等後續語音，直接推進問答串流**。

### 改造 C：LLM ➔ TTS「首小句切片串流（First-Clause Streaming）」

* **現狀**：目前 LLM 串流切句器（`sentence_splitter`）是以完整標點（`。！？\n`）作為切句點。LLM 常輸出：「好啊，這件事我覺得其實沒那麼複雜...」，必須等整句話生成完畢（15~25 字）才丟給 TTS。
* **變更邏輯**：
  * 引進 **「首句短前綴切分機制（First Clause Split）」**：
    * 針對回應的第一個片段，遇到逗號 `，`、分號 `；` 或長度達到 **6 個中文字** 時，只要語意可斷，**立即作為第一片送進 TTS**！
    * 例如：「沒問題，」、「我想想，」、「這首歌是，」 ➔ 立刻送出第 1 段 TTS。
    * 第二段開始恢復一般的整句切分（避免音訊切太碎導致語調機械化）。
  * 效果：LLM 首段推論時間從 400ms 壓縮至 **150ms**；TTS 合成前綴短片段只需 **150ms**。

### 改造 D：連續對談下動態縮短 VAD Silence

* **現狀**：`RealtimeVADSink` 的靜音截斷閥值固定為群聊防誤觸參數（約 500ms~600ms）。
* **變更邏輯**：
  * 當系統進入「對談中（Active Dialogue）」狀態時，將該 speaker 的 VAD silence threshold 動態降至 **300ms**。
  * 使用者講完閉嘴 300ms 立刻觸發 flush，節省 **200ms** 的空白等待。

---

## 3. 實作切分與 PR 計畫（給 Claude Code 實作指引）

### PR①：對談狀態識別與 Cleaner Bypass（預期延遲下降 400ms）

* **目標**：在喚醒狀態與 Follow-up 視窗內，Bypass 掉 `clean_for_worker`。
* **涉及檔案**：
  - [`cogs/voice_controller.py`](file:///Users/jackhuang/Code/Discord-voice-bot/cogs/voice_controller.py)（`_confirmation_flow`、`_process_query_task`）
  - [`etd_clean_reuse.py`](file:///Users/jackhuang/Code/Discord-voice-bot/etd_clean_reuse.py)
* **TDD 步驟**：
  1. 新增測試檔 `tests/test_fast_conversation_cleaner_bypass.py`：
     - Mock `ctrl.bot.router.clean_stt_text`。
     - 模擬在 `_wake_session_captured(speaker) == True` 狀態下呼叫 `_confirmation_flow`。
     - **斷言**：`clean_stt_text` 的 `call_count == 0`，且 `pipeline_timing` 立即打點 `cleaner_done`。
  2. 修改實作：在 `_confirmation_flow` 前段判斷對話活躍標記，直接 `return stripped`。
  3. 驗證全綠。

### PR②：首句短前綴切片串流 TTS（預期延遲下降 350ms）

* **目標**：優化 `stream_fast_response` 的首句截斷，讓第一段 TTS 提早起跑。
* **涉及檔案**：
  - [`gemini_router.py`](file:///Users/jackhuang/Code/Discord-voice-bot/gemini_router.py) 或文字切片器模組（搜尋 `sentence_splitter` 或 `_split_sentences`）
  - [`cogs/voice_controller.py`](file:///Users/jackhuang/Code/Discord-voice-bot/cogs/voice_controller.py)（`stream_fast_response` 串流迴圈）
* **TDD 步驟**：
  1. 新增測試檔 `tests/test_first_clause_streaming_splitter.py`：
     - 輸入串流 token：`["好的", "，", "我", "現在", "幫", "你", "找", "這首", "歌", "。"]`
     - 測試切句 generator 輸出的第一個 chunk 必須是 `"好的，"`，第二個 chunk 才是 `"我現在幫你找這首歌。"`。
  2. 修改實作：加入 `is_first_chunk` 狀態旗標，首句放寬標點為 `，,` 與字數門檻（`>=4` 字）。
  3. 驗證全綠。

### PR③：短指令預出價跳過等待（消除指令逾時卡頓）

* **目標**：解決 2~3 字短指令（如「下一首」、「閉嘴」）卡在 `awaiting_question` 逾時問題。
* **涉及檔案**：
  - [`cogs/voice_controller.py`](file:///Users/jackhuang/Code/Discord-voice-bot/cogs/voice_controller.py)（`_confirmation_flow`）
  - [`intent_bus.py`](file:///Users/jackhuang/Code/Discord-voice-bot/intent_bus.py)
* **TDD 步驟**：
  1. 新增測試檔 `tests/test_short_command_instant_bid.py`：
     - 模擬使用者喚醒後只講 `"下一首"`（3 字，小於原本 4 字門檻）。
     - 斷言：不進入 `evt.wait()` 等待，而是立即被 PlaybackControlAgent 接收。
  2. 修改實作：整合 IntentBus pre-bid 偵測。

---

## 4. 驗收標準（Acceptance Criteria）

1. **單元測試全數通過**：
   ```bash
   python3 -m pytest tests/test_fast_conversation_cleaner_bypass.py tests/test_first_clause_streaming_splitter.py tests/test_short_command_instant_bid.py -q
   ```
2. **日誌指標驗證**：
   - 觀察 `bot_main.log` 中的 `⏱️ [Latency-1]` 與 `wake_to_llm_ms` / `llm_to_sentence_ms` 打點：
   - 喚醒後的對談句：`wake→first_audio_ms` 必須 **≤ 850ms**（網路正常情況下）。
3. **無迴圈與誤觸**：
   - 連續對談時不噴多餘的 Filler。
   - 使用者說話完畢後，Marvin 在 1 秒內搶先回應，且第一句內容不結巴、不斷詞。

---

## 5. 交接給 Claude Code 的執行提示

* 請優先閱讀本檔案與 [`ARCHITECTURE.md`](file:///Users/jackhuang/Code/Discord-voice-bot/ARCHITECTURE.md)。
* 嚴格遵守工作區規定：**「先寫失敗測試確認紅燈，再寫最小實作，最後全綠」**。
* 改完後手動執行單元測試，並確認所有 mock 行為符合現有架構。

---

## Eng Review 記錄（/plan-eng-review，2026-10-22）

審查目標（固定）：`docs/PLAN_fast_followup_conversation.md`。比對基準：prod e6839b9（含 PR#139 對談窗跳 rescue、gap classifier 預設關）。

### 前提表（premise-table）
| # | 計劃書前提 | 實測／程式證據 | 判定 |
|---|---|---|---|
| P1 | 端到端 1.6–2.8s | `⏱️ [Latency-1]` 10/21 00:14 後 n=13：wake→llm p50 2.1s／p90 5.4s；llm→首句 p50 6.2s／p90 30.0s；合計 p50 8.8s（尚未含 TTS） [measured: bot_main.log] | FALSE |
| P2 | `_confirmation_flow` 的 clean_for_worker 300–600ms 是巨大痛點 | pipeline_timing 樣本 question_done→cleaner_done 6.5ms；clean_for_worker 只重用 Semantic ETD 結果（記憶 project_fast_followup_routing，10ms）；真正 LLM 清洗在入隊前的 ETD（約 1s） | FALSE |
| P3 | IntentBus ≤5ms | 記憶實測 1.2s（judge race）；本輪未重量 | UNVERIFIED（傾向 FALSE） |
| P4 | LLM 首句 350–600ms | 同 P1：p50 6.2s。模型 `gemini-3.1-flash-lite-preview`（gemini_router.py:117）；10/21–22 多次 503 後才走 stream_llm 備援（gemini_router_llm.py:976–979） | FALSE |
| P5 | TTS 首段 350–550ms | first_audio 打點從未接上（記憶） | UNVERIFIED |
| P6 | VAD 結句 450ms，可安全降到 300ms | 2026-07-05 n=2948 實測：中溫 1.5→1.2 誤切 12.1%、→1.0 誤切 17.8%，裁決全域收緊 NO-GO（記憶 vad_tail_measurement_verdict） | FALSE（與既有決定衝突） |
| P7 | 2–3 字短指令會卡在等問句逾時 | `_confirmation_flow` 已豁免 skip/pause/resume/stop（cogs/voice_controller.py `_detect_music_command(stripped) in ("skip","pause","resume","stop")`）；完整指令在入隊前由 WakeShortcut 直派（:1255–1281）。只剩「閉嘴」這類 <4 字非音樂指令會等 | 大部分已完成 |

### 實測瀑布（主 LLM 聊天路徑，p50）
```
STT ~0.3s ─▶ Semantic ETD（Groq）~1.0s ─▶ 入隊→LLM 開始 2.1s ─▶ LLM 首句 6.2s ─▶ TTS（未量）
                 └ 判未完成再硬等 2.5s                         └ 503 時等失敗才切備援；p90 30s
```

### 範圍紀錄（Scope Challenge）
複雜度：計劃書 3 個 PR、約 4 個檔、0 個新 class，未觸發複雜度門檻（<8 檔且 <2 個新 class）。範圍題改以 D1.1–D1.6 逐項決定。

### Decision ledger（範圍逐項，D1 split chain）

#### R1: 改造 A 對談中跳過 cleaner
Finding: SC-1，P1，confidence 9/10，計劃書 §2 改造 A 引 cogs/voice_controller.py:2589 `clean_for_worker`；實測 question_done→cleaner_done 6.5ms。
Plan baseline: 原提案 Include。
Runtime evidence: P2。
Options: A) Include B) Defer C) Cut (recommended) D) Hold
State: approved
Actual answer: C) Cut（使用者 2026-10-22，D1.1）
Accepted scope: 從計劃書移除改造 A／PR①；清洗成本改由 R6 處理

#### R2: 改造 B 短指令預出價跳過等待
Finding: SC-2，P3，confidence 8/10，P7。
Plan baseline: 原提案 Include（PR③）。
Runtime evidence: P7；剩「閉嘴」類 <4 字非音樂指令。
Options: A) Include B) Defer C) Cut (recommended) D) Hold
State: approved
Actual answer: C) Cut（使用者 2026-10-22，D1.2）
Accepted scope: 從計劃書移除改造 B／PR③；「閉嘴」類 <4 字非音樂指令維持現行

#### R3: 改造 C 首小句切片串流
Finding: SC-3，P2，confidence 7/10，P4/P5：LLM 首句 p50 6.2s 時，切片省的約 150–300ms 看不出來；TTS 首段沒量過。
Plan baseline: 原提案 Include（PR②）。
Runtime evidence: P5 UNVERIFIED。
Options: A) Include B) Defer (recommended) C) Cut D) Hold
State: deferred
Actual answer: B) Defer（使用者 2026-10-22，D1.3）
Accepted scope: 不在本波；觸發＝R5（E）上線且 TTS first_audio 打點接上後，用實測首句→出聲時間決定

#### R4: 改造 D 對談中 VAD 縮到 300ms
Finding: SC-4，P1，confidence 9/10，P6 與 2026-07-05 NO-GO 衝突。
Plan baseline: 原提案 Include。
Runtime evidence: P6。
Options: A) Include B) Defer C) Cut (recommended) D) Hold
State: approved
Actual answer: C) Cut（使用者 2026-10-22，D1.4）
Accepted scope: 從計劃書移除改造 D；維持 2026-07-05 全域不收緊 VAD 的決定

#### R5: 新增 E 主 LLM 首句拆解＋503 快速切換
Finding: SC-5，P1，confidence 8/10，P1/P4：首句 p50 6.2s、p90 30s，是目前最大一段。
Plan baseline: 計劃書沒有此項。
Runtime evidence: P4。
Options: A) Include (recommended) B) Defer C) Cut D) Hold
State: approved
Actual answer: A) Include（使用者 2026-10-22，D1.5）
Accepted scope: 本波第一刀。PR E1＝stream_fast_response 分段打點（組 prompt／雲端首字／切句器首句）＋503 快速切備援；上線量 1～2 天後依最慢段另定 E2 修正

#### R6: 新增 F 對談窗內放寬 Semantic ETD
Finding: SC-6，P2，confidence 7/10，每句喚醒都先經 Groq ETD 約 1s，判未完成再硬等 2.5s（記憶 project_fast_followup_routing 下一刀之一）。
Plan baseline: 計劃書沒有此項。
Runtime evidence: pipeline_timing 樣本 stt_done→dequeued 約 1.1s。
Options: A) Include (recommended) B) Defer C) Cut D) Hold
State: approved
Actual answer: A) Include（使用者 2026-10-22，D1.6）
Accepted scope: 先從既有 log 量「對談窗內 Groq ETD 判未完成」比例，可接受才在窗內（in_conversation_window）跳過 Groq ETD、只用本地啟發式

### Section 1 架構發現

#### R7: F 跳過 ETD 後清洗成本會搬到 worker
Finding: ARCH-1，P1，confidence 9/10。cogs/voice_controller.py `_apply_semantic_etd` 的 Track B-2 就是 `res = await self.bot.router.clean_stt_text(combined_text)`，結果由 `remember_etd` 存給 worker；etd_clean_reuse.py:53-69 `clean_for_worker` 在 `take_etd` 落空時會 `await asyncio.wait_for(ctrl.bot.router.clean_stt_text(stripped), timeout=ctrl._CONFIRM_CLEAN_TIMEOUT)`。只跳過 ETD 的 B-2，約 1s 會原封搬到 worker。
Plan baseline: R6 approved（D1.6）：窗內跳過 Groq ETD、只用本地啟發式；R1 approved（D1.1）：改造 A（全域跳過 worker cleaner）已刪。
Runtime evidence: 上述程式碼（靜態讀取）。
Options: A) 窗內兩處都跳過（ETD B-2＋worker cleaner），raw 文字直接進 IntentBus (recommended) B) 窗內只跳 ETD 完整度判斷、清洗照做（幾乎不省時間） C) 先只量（窗內「判未完成」比例＋cleaner 改字比例），量完再決定
State: approved
Actual answer: A) 窗內兩處都跳過（使用者 2026-10-22，D2）
Accepted scope: in_conversation_window 為真時，ETD 不呼叫 Groq（只走 B-1 本地啟發式）、worker 也不呼叫 clean_stt_text（raw stripped 直接進 IntentBus）；前提是先量「窗內 Groq 判未完成比例」與「cleaner 改字比例」都可接受（門檻見 R9）；窗外不變

#### R8: E 雲端首字等待上限
Finding: ARCH-2，P1，confidence 9/10。gemini_router_llm.py `_stream_cloud`：建立串流 `timeout=8.0`、整段迭代 `async with asyncio.timeout(30.0)`；llm→首句 p90＝30013ms／30031ms 正好是 30s 逾時才切備援。送出前還有 `await self._acquire_cloud_rpm_slot()`。
Plan baseline: R5 approved（D1.5）：503 改快速切備援；等待上限未定。
Runtime evidence: Latency-1 兩筆 30.0s；10/21–22 多筆 503。
Options: A) 首字 3s 內沒來就取消、改走 stream_llm（Groq） (recommended) B) 1.5s 後平行開 Groq，誰先出字用誰 C) 只打點、先不設上限
State: approved
Actual answer: A) 首字 3 秒上限後切 Groq（使用者 2026-10-22，D3）
Accepted scope: _stream_cloud（或 stream_fast_response 的雲端分支）在 3.0s 內沒拿到第一個非空 chunk 就取消雲端串流、改走 stream_llm；已出字後維持現行 30s；計時含建立串流，不含 _acquire_cloud_rpm_slot 之前的 prompt 組裝

#### R9: F 的開工門檻（窗內跳過 ETD＋清洗）
Finding: ARCH-3，P2，confidence 8/10，R7 accepted scope 要求先量兩個比例。
Plan baseline: R7 approved（D2=A）。
Runtime evidence [measured: bot_main.log*，10/14 23:12 起 WakeSession 接住的 82 句]：窗內句前 15s 內 Groq 判未完成 1/82（1.2%）；ETD reuse 改字（不計標點）29/82（35%），抽 12 筆：有幫助 3（人民區→人民廣場×2、酸食炸→酸性炸）、改壞或捏造 4（祠堂→廟裡、groundeto→圓地豆、韋小世界→《韋小說》、句尾補「你測試呢竟」）、中性 5（哪裏→哪裡、贅字）。
Options: A) 門檻：判未完成 ≤10%，且抽樣中改壞數不少於有幫助數 → 本次量測通過，F 照 R7 開工 (recommended) B) 不做 F
State: approved
Actual answer: A)（使用者 2026-10-22「都照你建議，我同意」，涵蓋本次審查其餘建議）
Accepted scope: F 開工條件已滿足；上線後一週用 TurnOutcome 對照窗內回合的 executed 與 frustration，若窗內答偏抱怨增加則以 env 關閉（見 R10）

### Section 2 程式品質（必要實作，無新選擇）
- CQ-1 [P2] (confidence: 9/10) cogs/voice_controller.py `_apply_semantic_etd` 現為 40 statement（＝NEW_METHOD_MAX，0 餘裕）。F 不得在該 method 內加分支：把 `res = await self.bot.router.clean_stt_text(combined_text)` 改成呼叫 mixin 方法（例 `_etd_semantic_check(speaker, combined_text)`，窗內回 None＝視為完成），同一個 statement。
- CQ-2 [P2] (confidence: 9/10) etd_clean_reuse.py `clean_for_worker` 的窗內跳過放在該模組（不在 voice_controller），用 `ctrl.in_conversation_window(speaker, time.time())`，並 `pipeline_timing.mark("cleaner_done")` 照舊打點。
- CQ-3 [P3] (confidence: 8/10) F 依賴 `in_conversation_window`（分支 feat/conversation-window-seam 921f3c2，未合併）；F 的 PR 必須疊在它之後。

### Section 3 測試（必要測試，無新選擇）
```
CODE PATHS                                               USER FLOWS
[+] gemini_router_llm.stream_fast_response / _stream_cloud  [+] 對談中問 Marvin 問題
  ├── [GAP] 分段打點：prompt／rpm／首 chunk／路由 寫一行      ├── [GAP] 雲端正常：首字 <3s 照用雲端
  ├── [GAP] 首 chunk 3s 內到 → 照用雲端                      ├── [GAP] 雲端卡住：3s 後改 Groq，首句 <5s
  ├── [GAP] 3s 內沒 chunk → 取消雲端、走 stream_llm          └── [GAP] 雲端 503：立即走 Groq（現行已有 ★★）
  ├── [★★ TESTED?] 已出字後例外 → raise（不換模型重講）
  └── [GAP] 取消雲端時不留 dangling task／不記帳失敗
[+] F 窗內跳過
  ├── [GAP] in_conversation_window 真 → ETD 不呼叫 clean_stt_text、視為完成
  ├── [GAP] 窗外 → ETD 照舊呼叫
  ├── [GAP] 窗內 B-1 拖延詞仍判未完成（啟發式不被跳過）
  ├── [GAP] 窗內 clean_for_worker 回 raw、仍打 cleaner_done
  └── [GAP] env 關閉（R10）→ 全部照舊
```
- 首 chunk 上限用可控的假串流（asyncio.Event／sleep 注入）測，不用真的等 3 秒；不打真 API。
- 既有回歸：`stream_fast_response` 503 未出字走備援、已出字 raise 的現行行為要有測試鎖住（先找既有測試，沒有就補）。

### Section 4 效能
- PERF-1 [P2] (confidence: 7/10) gemini_router_llm.py `await self._acquire_cloud_rpm_slot()` 在首字計時內；E1 打點要單獨記 rpm 等待時間，否則 3s 上限可能被 RPM 排隊吃掉而誤切 Groq。決定：3s 計時從 `_acquire_cloud_rpm_slot()` 回來後開始（必要實作，對齊 R8「不含 prompt 組裝」）。
- PERF-2 [P3] (confidence: 6/10) `_context_injector.enrich` 與 `_execute_web_search` 在 LLM 前同步 await；E1 打點要分開記，E2 才知道要不要改平行。

#### R10: F 的 kill flag
Finding: 必要的回滾手段（窗內跳清洗是行為變更）。
Options: A) env `MARVIN_FASTLANE_SKIP_CLEAN=0` 關閉 F（預設開） (recommended) B) 不加 flag
State: approved
Actual answer: A)（使用者 2026-10-22「都照你建議，我同意」）
Accepted scope: 加 env flag，預設 "1"；"0" 時 ETD 與 worker 清洗都照舊；含測試

### Outside Voice（Claude Plan 子代理 native fallback；codex 未安裝）
11 條。已驗證：OV-1（兩筆 30s 都在 a93dcb7〔2026-10-21 23:16〕之前，是空的 Cloud Stream Error＋Late Skip；之後 0 筆卡住）、OV-3（`_acquire_cloud_rpm_slot` 在 `_stream_cloud` generator 內）、OV-8（`_process_queued_query`：`if _winner or await try_question_opener(self, _bus_ctx)`，無 winner 時先 await 3.5-flash 判問句才進主 LLM）、OV-10（tests/test_stream_fast_response_fallback.py 已存在）。處置依使用者 2026-10-22「都照你建議，我同意」套用建議：

#### R8（重開）: E 雲端首字等待上限
History: 原答 D3=A（3s 上限切 Groq）。重開理由：OV-1 證據顯示 p90 30s 是 a93dcb7 前的舊錯誤路徑，現行無卡住樣本；OV-2 無資料先定門檻可能把大部分流量切去 Groq；OV-4 跨 yield timeout 取消錯 task 的風險。
State: approved
Actual answer: 改為 E1 只打點、上限延到 E2（使用者授權套用建議）
Accepted scope: E1 不設首字上限；E2 若要設，必須 env 可調、用 `asyncio.wait_for(stream.__anext__(), remaining)` 取首 chunk 並在取消時 `aclose()`，且同時評估預取（`_speculative_response`）雙打與 Groq 30 RPM 共用、RPM 排隊（OV-3/OV-5）

#### R7 實作更正（行為不變）
F 改為：mixin `_etd_semantic_check` 在窗內回合成結果 `{"text": combined_text, "is_complete": True}`（不呼叫 Groq），既有 `remember_etd` 照存，worker `take_etd` 必命中；`etd_clean_reuse.py` 不改、CQ-2 刪除；單一判斷點、kill flag 只在 mixin（OV-6）。

#### R11: E1 打點範圍與 F 的驗收指標
Accepted scope（使用者授權套用建議）：
- E1 打點涵蓋 IntentBus 結束→`try_question_opener` 結束→主 LLM 開始（OV-8），以及 stream_fast_response 內 prompt 組裝（context injector／web search 分開）、rpm 等待、雲端首 chunk、路由；每回合一行 `⏱️ [LLM-Stage]`。
- F 的延遲指標＝pipeline_timing `stt_done→dequeued`（窗內 vs 窗外，上線前後各一週）（OV-7）。
- F 上線後監看窗內「同 speaker 3s 內接續第二句」次數（OV-9：1/82 有倖存者偏差），明顯上升就關 `MARVIN_FASTLANE_SKIP_CLEAN`。
- 測試擴充 tests/test_stream_fast_response_fallback.py（OV-10）；F 的 PR base 必須是 e6839b9 之後並疊在 feat/conversation-window-seam 上（OV-11）。

Approval readiness: PASS — R1（D1.1）、R2（D1.2）、R3（D1.3 Defer）、R4（D1.4）、R5（D1.5）、R6（D1.6）、R7（D2＋OV-6 實作更正）、R8（重開，使用者授權套用建議）、R9、R10、R11（使用者「都照你建議，我同意」）。

### 修訂後計劃（取代 §2–§4）
1. **PR E1 打點**（human ~半天 / CC ~30min）：R11 的 `⏱️ [LLM-Stage]` 一行；不改行為。上線量 1～2 天。
2. **PR F 對談窗跳 ETD＋清洗**（human ~1 天 / CC ~45min）：疊在 feat/conversation-window-seam；`_etd_semantic_check` mixin 回合成結果；`MARVIN_FASTLANE_SKIP_CLEAN`（預設 "1"）；測試見 Section 3。
3. **E2**：依 E1 數字挑最慢段（候選：try_question_opener 串行、context injector／web search 同步、雲端首字），另開審查決定。
4. **C（首小句切片）**：E 上線且 TTS first_audio 打點接上後再議。
已刪：改造 A、B、D。

### NOT in scope
- 改造 A 全域跳 cleaner（R1）、改造 B 預出價（R2）、改造 D VAD 300ms（R4，2026-07-05 NO-GO）。
- 首字上限（R8，延到 E2）、首小句切片（R3）。

### What already exists
- PR#139 對談窗跳 rescue、gap classifier 預設關；a93dcb7 503 未出字切 stream_llm；etd_clean_reuse（ETD 清洗重用）；WakeShortcut 完整指令直派；`in_conversation_window`（921f3c2）；tests/test_stream_fast_response_fallback.py。

### Failure modes
| 路徑 | 實際失敗 | 測試／處理 | 使用者看到 |
|---|---|---|---|
| E1 打點 | 計時例外 | 打點包 try、不影響串流 | 無 |
| F 窗內跳過 | 半句被當完整句 | B-1 啟發式仍生效＋kill flag＋接續句監看 | 偶爾答半句，可再補一句 |
| F 窗內 raw 文字 | 糊字進主 LLM／搜尋 | IntentBus 內 J3 cleaner 仍在；TurnOutcome 看窗內 frustration | 偶爾答偏 |
Critical gaps：0。

### Implementation Tasks
- [ ] **T1 (P1, human: ~半天 / CC: ~30min)** — LLM 延遲 — E1 分段打點（R11）。Files：gemini_router_llm.py、cogs/voice_controller_wake_session.py 或新 mixin（避開 _process_queued_query 凍結）。Verify：單元測試＋上線 log 每回合一行。
- [ ] **T2 (P1, human: ~1 天 / CC: ~45min)** — F — 窗內合成 ETD 結果＋kill flag（R7/R10）。Files：cogs/voice_controller.py（同 statement 改呼叫）、cogs/voice_controller_wake_session.py。Verify：Section 3 的 F 測試；stt_done→dequeued 窗內下降約 1s。
- [ ] **T3 (P2)** — E2 審查（依 T1 數字）。

Worktree parallelization：T1（gemini_router_llm）與 T2（ETD mixin）模組不重疊，可兩條 lane 平行；T2 需先合 feat/conversation-window-seam。

### REALITY rows
- REALITY: premise-table finding unverified=2（P3 IntentBus、P5 TTS） docs/PLAN_fast_followup_conversation.md:33
- REALITY: already-done finding 改造 B 主要情境已由豁免與 WakeShortcut 完成 cogs/voice_controller.py:1255
- REALITY: surface-check pass 無新 CLI；新 env MARVIN_FASTLANE_SKIP_CLEAN 由本計劃定義 etd_clean_reuse.py:18
- REALITY: binding-decisions finding 改造 D 與 2026-07-05 VAD NO-GO 衝突，已刪 docs/PLAN_fast_followup_conversation.md:88
- REALITY: numbers finding 原 1.6–2.8s 基準不成立；p90 30s 經 OV-1 證實為修正前資料 gemini_router_llm.py:737
- REALITY: deferred-asks pass 無連結 issue docs/PLAN_fast_followup_conversation.md:1
- REALITY: pr-count pass 原 3 PR → 2 PR（E1、F），E2 另審 docs/PLAN_fast_followup_conversation.md:97

### Unresolved decisions
無。

### Completion summary
- Step 0: Scope Challenge — scope reduced per recommendation（刪 A/B/D、延 C、加 E/F）
- Reality rows: 7/7 applicable rows emitted, 2 unverified premises
- Architecture Review: 3 issues found（R7 清洗耦合、R8 首字上限、R9 開工門檻）
- Code Quality Review: 2 issues found（CQ-1 ETD 棘輪、CQ-3 分支依賴；CQ-2 經 OV-6 刪除）
- Test Review: diagram produced, 10 gaps identified
- Performance Review: 2 issues found
- NOT in scope: written
- What already exists: written
- TODOS.md updates: 0 items proposed（既有 P1 已有註記，修訂後計劃取代）
- Failure modes: 0 critical gaps flagged
- Unresolved decisions: 0 in this review
- Outside voice: codex 未安裝 → native Claude Plan 子代理 completed（11 條；R8 重開、R7 實作更正、R11 新增）
- Parallelization: 2 lanes, 2 parallel / 0 sequential
- Lake Score: N/A（題目皆種類不同）

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Outside Review | codex 未安裝 → native Claude Plan subagent | Independent 2nd opinion | 1 | unavailable（native completed） | 11 條：R8 重開、R7 實作更正、R11 新增 |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | ISSUES OPEN | 17 issues, 0 critical gaps（全數已處置） |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **OUTSIDE COVERAGE:** codex，plan-review phase，unavailable（CLI 未安裝）；native Claude Plan 子代理 5 分鐘內完成，不計外部覆蓋。
- **VERDICT:** Eng Review ISSUES OPEN（17 條皆已處置、0 未決）—— 可依「修訂後計劃」動工 T1、T2。

NO UNRESOLVED DECISIONS
