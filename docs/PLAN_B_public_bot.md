# Plan B — Marvin 公開可邀請 bot（self-host，他人 invite）

> **核心原則：有客戶再開始。** 這份計劃是「冷凍待命」的——在 §0 啟動條件滿足前，一行 code 都不寫。
> Marvin 的價值是「多人房間裡的智慧語音夥伴」，B 的賭注是病毒式分發（每個房間都是公開 demo），
> 代價是**所有房間的運算成本集中算在 host 頭上**（與 Open-LLM-VTuber 的「用戶自付」經濟模型相反）。
>
> 資料基準日：2026-06-06。Gemini 報價：2.5 Flash `$0.30/M in` `$2.50/M out`（[ai.google.dev](https://ai.google.dev/gemini-api/docs/pricing)）。

---

## §0 啟動條件（trigger gate）— 全滿足才解凍

不是「想做就做」，是這四個訊號**同時亮**才動工：

1. **需求訊號**：≥3 個非自己人的伺服器房主**主動表示**想把 Marvin 加進他們的群（不是你猜，是他們開口）。
2. **成本覆蓋**：每活躍 guild 邊際成本 ≈ **$20/月**（見 §1）。啟動前要想清楚誰買單——
   自掏腰包上限 / 訂閱方案 / 贊助。**沒有覆蓋方案＝不啟動**（你已撞過 spending cap，B 會放大它）。
3. **本地台已穩**：home guild 的 Marvin 連續 2 週無 crash loop / 無 STT 全死事故（B 是把不穩定 ×N）。
4. **你有時間吃 ops**：B = 你變成別人的 SLA 提供者。沒有每週能投入維運的時間就不要開。

> 任一條沒亮 → 維持「self-host 自己玩 + 朋友手動加（Plan A 路徑）」，不碰 B。

---

## §1 成本預估 — 本地台「全走 Gemini」用真實資料算

### 真實用量（單一實例 ≈ 1 活躍 guild，忙日）

| 指標 | 真實值 | 來源 |
|---|---|---|
| 使用者轉錄/忙日 | **~1,600–2,150 句** | `stt_*.log` 數 `(Debounced)` 行（06-02=1596, 06-03/04=2136, 淡日 06-05=707）|
| marvin_chat 主回應 | **~150–330/日** | `llm_routing.jsonl`（06-02=330, 06-03=168, 05-31=142）|
| cleaner 呼叫 | **~8–22/日（gated）** | 多數轉錄只被動聽餵 context，**不**送 LLM；只有喚醒/指向 Marvin 的才清理 |
| 背景任務 | **~100/日** | song reactions / emotional moments / greetings / summaries |
| marvin_chat latency 中位 | 550ms (n=1815) | 真實 |

> ⚠️ **修正紀錄**：初版誤把轉錄寫成 ~200 句（grep 數錯欄位）。真實 ~2,000 句/忙日，現由
> **Swift 本地免費分攤**；「全走 Gemini」= 這 2,000 句全推雲。但初版同時把 cleaner 高估成 200/日
> （真實 gated ~20），兩錯反向抵消，**$20/guild/月 headline 不變**——錯只錯在 STT 那一行的組成。

> ⚠️ **資料純度註記**：呼叫「量」是真實的；**每筆 token 數是估的**——`llm_routing.jsonl`
> 記 `tokens:0`（沒記 token）。下表 token 假設來自 prompt 靜態大小（`marvin_prompts.py` ~7K token 庫、
> 實際注入子集 + 5–10 輪歷史 + memory）。central 估計，非精算。

### 成本拆解（Gemini 2.5 Flash，單實例忙日）

| 類別 | 量/日 | in tok/次 | out tok/次 | in 成本 | out 成本 | 小計 |
|---|---:|---:|---:|---:|---:|---:|
| marvin_chat | 200 | 4,000 | 120 | $0.240 | $0.060 | **$0.30** |
| 背景任務 | 100 | 2,500 | 250 | $0.075 | $0.063 | **$0.14** |
| STT（音訊→Gemini）| ~2,000 句×~3s×32tok/s | — | — | $0.058 | — | **~$0.06** |
| cleaner | ~20 | 1,000 | 40 | $0.006 | $0.002 | **~$0.01** |
| **單實例忙日合計** | | | | | | **≈ $0.51** |

- **TTS 不算**：edge-tts 免費，保留（不走 Gemini TTS）。
- **STT $ 便宜但別「全走 Gemini」**：~2,000 句 ×3s ×32tok/s ≈ 192K tok ≈ $0.06/日，$ 不是問題；真正問題是延遲。
- **⚠️ Groq vs Swift 已實測 A/B（2026-06-06，`scripts/stt_swift_vs_groq_ab.py`，n=4 真實 WAV）**：
  **Swift 單句完勝**——中位 Swift 343ms vs Groq 525ms（Groq 慢 ~1.5x，網路 RTT 吃掉其運算優勢）；
  Swift 對音長幾乎免疫（2s→10.5s 只 268→410ms），Groq 隨上傳量長到 1024ms。
  Groq 還有兩坑：**簡體輸出**（饿/这，zh-TW 要再轉換）、這組樣本**品質更差**（2s 噪音幻覺、10.5s 掉內容）。
  caveat：n=4 品質結論弱；我送原始 48kHz stereo，降 16kHz mono 會讓 Groq 快一點/可能變好，但 RTT 地板還在、Swift 仍贏單句。
  → **修正關鍵假設**：Phase 1 走 Groq **不是升級、是「降級換可移植性」**。Swift 更快更準但 macOS 綁死 + `Semaphore(1)` 序列化（你 06-06 breakdown「排隊等 worker p50=415ms」單 guild 就露）。
  **Groq 對 B 的唯一價值＝能上 Linux + 雲端並行不序列化，不是延遲。** 若 Swift 能上雲，B 根本不需要 Groq。
- **wake-check 隱藏量未實測**：丟掉本地 Swift 後，每段語音的喚醒檢查若也走雲端 STT，量比 2,000 句更大 + 熱路徑延遲，啟動前要先量。
- **成本主力 = marvin_chat 的 input**：大系統 prompt 每輪重送（context caching 是最大槓桿）。

### 換算與外推

| 情境 | 估算 |
|---|---|
| 單實例忙日 | **~$0.5–1.0/日**（token 估計區間 + 更忙的天）|
| 單實例/月（每日活躍 ~25 天）| **~$15–30/月** |
| **規劃用數字** | **≈ $20 / 活躍 guild / 月** |
| B：50 活躍 guild | ~**$1,000/月** |
| B：200 活躍 guild | ~**$4,000/月** |
| B：1,000 活躍 guild | ~**$20,000/月** |

> **這就是 §0 條件 2 的由來**：B 的邊際成本 ≈ $20/活躍-guild/月，**線性、上不封頂、全算在你頭上**。

### 把數字壓下去的槓桿（啟動後才做）

1. **Context caching**：把人格/系統 prompt 快取 → 砍 marvin_chat input ~50–70%（最大一筆）。
2. **Flash-Lite 跑 cleaner/背景** → 那兩類 ~3x 便宜。
3. **Batch API 跑背景任務**（非即時）→ 再 50% off。
4. 三者疊加可把 $20 → **~$6–10/guild/月**。但**這是優化，不是止血**——止血是 §3 配額閘。

> **反向風險**：現在的量被免費池 429 壓著（log 滿是限流）。付費後**更多呼叫會成功 → 實際可能比估計高 1.5–2x**。規劃時抓 **$30/guild/月** 上緣較安全。

---

## §2 分階段實作 + 資源整合

每階段：目標 / 工作 / 要 provision 的資源 / 驗證點。**前一階段驗證綠才進下一階段。**

### Phase 1 — 主機基座（兩條路，早期選 A）

> **⚠️ 重大修正（2026-06-06，實測驅動）**：原本預設「上 Linux + Groq」，但兩個實測翻案：
> ① Swift 單句完勝 Groq（343 vs 525ms、且 zh-TW 非簡體）；
> ② Swift **能並行**（`scripts/stt_swift_concurrency.py`：M1 8GB 上 N=8 batch 僅 1468ms，非 OS 序列化；`Semaphore(1)` 是軟體選擇可調）。
> → **Swift 的唯一死穴只剩「macOS 綁死」**。那就別逃離 macOS——用 Mac mini 當 host 即可全保留 Swift。

**路 A — Mac mini host（早期首選）**
- **目標**：一台 M4 Mac mini 當 host，**完整保留現有 macOS stack（Swift STT / davey / edge-tts / ffmpeg）**，零 STT 重寫。
- **為什麼**：Swift 更快更準免費；M4（16-24GB 不 swap + NE ~3x）保守估 2-3x M1 容量 → **錯開 duty cycle 可服務低數十個活躍房間**，早期 B 綽綽有餘。
- **工作**：`Semaphore(1)→Semaphore(N)` 放開 STT 並行（先量 N 對準確度/RAM 副作用）；其餘走 Phase 2 去單例。
- **資源**：一台 M4 mini（自家 ~$600 一次性，或雲端 Mac colo ~$100-200/月）。**無 STT API 成本**。
- **驗證點**：單 mini 上 3-4 個並發語音房同時對話，STT p50 < 600ms、無 swap。
- **代價/上限**：固定月成本高於 Linux VM；水平擴張＝加 Mac（非彈性 autoscale）。**真爆量才轉路 B。**

**路 B — Linux + 雲端 STT（後期/大規模才需要）**
- **何時**：活躍房間數超出單 Mac mini、或需要全球彈性擴張。
- **工作**：`Dockerfile` 收尾（已排除 macOS 套件）；STT 走 Groq（**降級換可移植性**，非升級）；**驗 DAVE/davey 在 Linux 能解密**（[[cryptoerror_storm_sentinel_blindspot]]）。
- **資源**：Linux VM（$20-40/月）+ Groq key（$0.04/hr STT）。
- **若認真考慮路 B 才測**：faster-whisper / FunASR GPU 是否比 Groq 快又不綁 mac → 租雲端 GPU 按小時測（RunPod/Modal/Replicate），harness 仿 `stt_swift_vs_groq_ab.py` 指向雲端 endpoint。**Mac mini 決策未定前別測。**

### Phase 2 — 去單例 + per-guild 路由（核心重構）
- **目標**：拆掉「只有一個活躍房間」假設。
- **工作**：
  - `DiscordVoiceEngine` 單例（`main_discord.py:215`）→ per-guild 實例（或 engine 內 per-guild 狀態）。
  - 所有 `voice_clients[0]` / `self.voice_client`（`discord_voice_engine.py:736`、`companion_bridge.py:227`、voice_controller 多處）→ **按 `guild_id` 路由**。
  - `stt_lock = Semaphore(1)` → **per-guild lane（每房 ~3 條，人類同時開口上限）**。
  - `[Core_*]` log 全加 guild tag；一個 guild 例外不拖垮其他（per-guild try/except 隔離）。
- **資源**：純工程，無新基建。
- **驗證點**：兩個測試 guild 同時語音對話，互不阻塞、互不串話、狀態互不污染（per-guild 測試）。

### Phase 3 — 多租戶治理 + 止血閥（B 的生死）
- **目標**：陌生人進來不會燒爆你錢包 / 不違規。
- **工作**：
  - **Admission control**：同時最多服務 K 個活躍語音房，滿了排隊/婉拒（K 由 VM 容量定，初期保守如 K=5–8）。
  - **Per-guild 配額 + rate limit**：單 guild 日 LLM 上限，超了降級貼文/拒答（接 `llm_pool` 既有歸因）。
  - **Per-guild consent gate**：進 STT 前強制（`consent.json` 已 per-guild 化基礎，見 [[runtime_state_files]]）+ ZDR 套用（見 [[project_relaxed_zdr_tiered_retention]]）。
  - **De-pin home guild**：拔 `GUILD_ID` / `COMPANION_GUILD_ID` / `TEMP_TEXT_CHANNEL_ID` 寫死，suki_memory home 特殊性中性化。
- **資源**：配額 store（SQLite/redis）、基本監控儀表（每 guild 用量/成本）。
- **驗證點**：模擬一個「爆量惡意 guild」→ 被配額擋下、其他 guild 不受影響、總成本封頂。

### Phase 4 — 公開化 + 邀請流
- **目標**：別人能自己 invite + onboard。
- **工作**：OAuth public bot listing、邀請連結、onboarding（invite → consent 同意 → 選頻道）、（若收費）方案/計費接入。
- **資源**：landing page、（若收費）金流。
- **驗證點**：一個外部房主從零 invite → 同意 → Marvin 進語音 → 正常對話，全程零你介入。

### Phase 5 — 規模化（只在真有量時）
- **目標**：超出單 VM 容量才做。
- **工作**：voice worker 分片（每 worker 扛 K guild）、guild→worker 路由、共享狀態層。Discord >2500 guild 需 sharding。
- **資源**：多 worker、路由/狀態服務。
- **驗證點**：加一個 worker → 容量線性上升、無狀態錯亂。
- > **別提早做**：YAGNI。Phase 1–4 撐得住前幾十個 guild。

---

## §3 風險與止血閥（一句話版）

| 風險 | 止血 |
|---|---|
| 成本上不封頂（$20/guild/月 ×N，全你付）| §0 條件 2 成本覆蓋 + Phase 3 配額/admission（**非優化，是閥**）|
| M1 8GB 物理撐不住 | Phase 1 上 Linux + 雲端 STT（已半鋪好）|
| 陌生人語音 = 你變資料處理者 | Phase 3 per-guild consent + ZDR |
| 單 guild bug 拖垮全部 | Phase 2 per-guild 隔離 |
| 你變別人的 SLA、被維運綁死 | §0 條件 4：沒時間就不開 |

---

## 附錄：資料來源 & 假設

- **真實**：呼叫量（`records/llm_routing.jsonl`）、轉錄量（`records/daily/stt_*.log`）、latency。
- **估計**：每筆 token 數（log `tokens:0` 未記）→ 來自 prompt 靜態大小推估，central 值，非精算。
- **報價**：Gemini 2.5 Flash $0.30/$2.50 per M（2026-06，可能變，重算前先查 [pricing](https://ai.google.dev/gemini-api/docs/pricing)）。
- **未計**：Gemini TTS（保留 edge-tts 免費）、context caching/batch 折扣（§1 槓桿，啟動後才套）。
