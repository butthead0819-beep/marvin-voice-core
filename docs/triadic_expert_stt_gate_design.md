# 三角專家投票 — positive / negative / biased（架構原則 + STT gate 個案）

> 狀態：**用在 cleaner-gate 域 = NO-GO；但模式本身是對的**（2026-06-04 reframe）。
> 日期：2026-06-04
> 關聯程式：[`stt_cleaner.cleaner_gate_decision`](../stt_cleaner.py)、`clean_stt_text`
> 的 tier chain、`TieredLLMRouter`；wake 系統 [`wake_detector.WAKE_WORDS_LIST`](../wake_detector.py)、
> [`analyze_daily_log.filter_unsafe_wake_additions`](../scripts/analyze_daily_log.py)。

---

## ★ Reframe：三專家模式是對的，只是 cleaner-gate 是錯的域

最初為 cleaner gate 設計三專家投票（E1 positive / E2 negative / E3 learned bias），
判 NO-GO。後來發現**同一個結構一直活在 wake 系統，而且在那裡 work**：

| 三專家 | wake 系統 | 角色 |
|---|---|---|
| E1 positive（我/播/聽/想/要） | `WAKE_WORDS_LIST` | 「這是意圖」正向 pattern |
| E2 negative（你/不/別/停） | `removals guard`（wake_words_override.json） | 「這不是」否決 |
| E3 biased（歷史 true/false bias） | `addition guard`（filter_unsafe_wake_additions，drops 頻率） | 從負空間學、curate 名冊 |

**模式成立的兩個條件**（cleaner-gate 兩個都不滿足，wake 都滿足）：

1. **域：離散穩定 token + 正負類乾淨可分。**
   wake 詞短、重複；合法近音詞在 `cleaner_gate_drops` 出現 **0 次**、日常詞 7 次 → 鴻溝清楚。
   cleaner gate 是糊掉整句（48% 兩專家都不中、同 raw 對到不同 clean）→ 正負類糊在一起，
   投不出信號（糊 raw 上 keyword 投票＝雞生蛋）。

2. **時機：把 biased/learning expert 拆去離線。**
   原始提案想三專家**同時 per-utterance 對糊 raw 投票** → 死。
   wake 系統拆開：E3 在**離線/每日/aggregate** curate 名冊（昂貴+雜訊的學習丟離線），
   E1/E2 在 **runtime/per-utterance** 只做乾淨離散比對。runtime 快、學習慢，各得其所。

> **可複用原則**：positive/negative/biased 三專家對的；但只在 (a) 離散穩定 token、
> 正負類可分的域，且 (b) biased expert 拆去離線 curate、positive/negative 留 runtime 時成立。

下面是原始 cleaner-gate 個案的完整分析（保留供參，結論仍是該域 NO-GO）。

---

## 0. 一句話

三個 expert 對 raw STT 投票出一個**機率式 intent 分數**。
**投票只在 quick pool 冷卻（配額爆 / p90 19-27s 的壞日子）時啟動**——
平常日 quick 亞秒回應，零干預、不掉任何意圖；壓力下才用分數決定
「哪些句子值得排隊等稀缺算力、哪些直接落 raw 不燒 6s 預算」。
E3 與關鍵詞由每日分析持續校準。這是**條件式止血節流**，不是全面 gate。

## 0b. 為什麼放「入口（壓力時）」而非「tier 升級點」（2026-06-04 查 code 結論）

直覺會想把投票放在「quick→analyze 升級」這個最慢的決策上。**查 code 否決**：
[`stt_cleaner.py:339-348`](../stt_cleaner.py) 升 analyze 的條件是 **quick 回 None
（整池 429 冷卻 / timeout），不是 quick 品質不夠**——quick 一吐出 JSON 就直接 return。
所以最慢的 p90 來自「沒算力」，投票判「這句值得升 analyze」也變不出算力，無效。
→ 能省的位置是**入口**（要不要進 quick chain），且只在**壓力時**省才划算。

---

## 1. 為什麼不照原始 2×2 硬表（資料打臉）

原規格：E1✓E2✗→送、E1✗E2✓→不送、E1✓E2✓→E3決定。
跑在 **2031 筆真的送過 cleaner 且被改過的 raw**（`records/stt_corrections.jsonl`）上：

| 投票格 | 原決策 | 真實佔比 | 結論 |
|---|---|---|---|
| E1✓ E2✗ | 送 | 25% | OK |
| E1✗ E2✓ | 不送 | 14% | ⚠️ 都是真被改過的句子 |
| E1✓ E2✓ | E3 | 14% | 需要 E3 |
| **E1✗ E2✗** | （未定義） | **48%** | 🔴 硬砍 = 掉半數真流量 |

三個硬傷：
1. **neither 48% 盲區**：最糊、最需要 cleaner 的句子，正好 E1 在 raw 上抓不到
   （「播放」被 STT 截成「波/薄」），keyword gate 的雞生蛋問題照樣存在。
2. **E2 的「你」是 bug**：「你播放周杰倫」「你放下一首」是對 Marvin 的真命令，
   「你」在口語是呼叫對方做事，不是拒絕。E2-only 14% 多半是這種誤殺。
   → **E2 去掉「你」**，只留「不/別/停」（且「不要停/別跳過」本身是命令，見 §3）。
3. **E3 沒有它要學的標籤**：corrections 只有 raw→clean、且只記「通過 gate 的句」，
   倖存者偏誤；學不到 gate 邊界。

→ 改為**機率式分數 + 分級降級**（與使用者「機率式評分」原話一致）。

---

## 2. 分數模型（取代硬表）

```
score(raw) = w1 * E1(raw) − w2 * E2(raw) + E3_bias(raw)
```

- **E1（positive，self-action）**：raw 命中 {我, 播, 聽, 想, 要} 的加權命中度（可帶詞頻權重，
  不是 0/1）。代表「在講自己 + 要執行動作」。
- **E2（negative）**：raw 命中 {不, 別, 停}（**移除「你」**）。降低分數，但**不歸零**
  （見 §3 為何 E2 不能當硬否決）。
- **E3_bias（learned，§4）**：每個 token 的「事後真意圖相關性」學習權重，每日更新。
  初期全 0（退化成純 E1/E2），有資料後才起作用。

### 觸發閘（pressure gate）— 投票何時啟動

```
under_pressure = quick pool 全部 endpoint 都在 cooldown
                 （llm_pool.CooldownAwarePool 已有 per-endpoint cooldown_until，
                  加一個 quick_pool.all_cooling() -> bool 即可）
```

- `under_pressure == False`（平常）：**不投票**，每句照現有流程送 quick。
- `under_pressure == True`（壞日子）：投票生效，按下表分級。

### 分數 → 路由（僅 under_pressure 時）

| score 區間 | 路由 | 理由 |
|---|---|---|
| 高（≥ τ_high） | **排隊等 quick / 升 analyze**，給滿 6s 預算 | 高信心意圖，稀缺算力優先給它 |
| 低（< τ_high） | **直接 raw passthrough**，不燒預算 | 壓力下不值得為低意圖句等 6s |

- 壓力下本來句子最終多半就落 raw（quick 死了）。投票只是把「該等的等、不該等的快放」，
  **嚴格優於現在每句盲目燒 6s**。投票錯判代價被壞日子攤平（很多本來就要落 raw）。
- 壓力下只需**二分**（等 / 不等），不需平常日的三檔精度 → E1 雞生蛋容錯高。
- **現有 `cleaner_gate_decision` 的 wake/music/ctx/spoke 仍是前置硬放行**：命中這些訊號的句子
  即使壓力下也歸「高」桶（至少排隊等），**投票不能把安全網訊號降到 raw**（對齊 Injection
  Guard 精神）。投票只對「過了 gate、但無硬訊號」的灰色句子做等/不等的排序。
- τ_high 由 §5 shadow 定，初期保守（門檻低 → 壓力下仍多數排隊，隨 E3 變準再上調）。

---

## 3. 為什麼 E2 是 de-prioritizer，不是硬否決

「不要停」「別跳過」「不想聽這首」——都含 E2 詞，**卻是真命令**。
若 E2 命中就硬砍，會誤殺「否定式命令」。所以 E2 只**扣分**，
讓句子傾向 quick-only 而非直接 raw；要掉到 raw 必須同時 **E1 不中 + E3 低 + 非 ctx**。
這樣「不要停」因為通常在 ctx_active（對話中）→ 前置安全網保底 quick-only，不會被丟。

---

## 4. E3 的 learned bias — 標籤從哪來（關鍵未解項）

E3 要學「given raw token → P(這句最後是真意圖)」。現有資料缺這個 label。
**設計解法：補一個 downstream outcome label，建新的訓練 join。**

每次句子過 cleaner，多記一筆 outcome（新 log，沿用 design_disciplines「pure core + IO shell」）：
```
{ts, raw, cleaned, changed: bool,
 resolved_intent: bool,   # cleaned 是否 is_wake=True 或觸發了 music/command agent
 tier_used: "full"|"quick"|"raw"}
```
`resolved_intent` 在 pipeline 下游本來就知道（IBA fusion / IntentBus dispatch 的結果），
只是現在沒回寫到 cleaner outcome。這是唯一需要的新接線。

**每日分析（接 daily ritual）做兩件事：**
1. **擬合 E3 token 權重** = 該 token 在 `resolved_intent=True` vs `False` 的對數勝率
   （簡單 Naive-Bayes-ish，不需重模型）。低樣本 token 收斂到 0（不亂動）。
2. **校準 E1/E2 詞表**：
   - 某 token 與 `resolved_intent` 高正相關但不在 E1 → **建議加入 E1**（人審）。
   - E1/E2 內某 token 相關性接近 0 或反向（如「你」）→ **建議移除/降權**。
   - 對齊 [[feedback_intent_gap_threshold]]：累計 2 次同類證據即標 ready。

⚠️ **bootstrap 期**：E3 權重全 0，系統 == 純 E1(−E2) 分數 + 保守門檻。
資料夠（建議 ≥ 1 週、≥ 數百筆有 outcome）才讓 E3 生效。對齊
[[feedback_env_gated_shadow_verify]]：先確認 outcome log 真的有在寫，再開 E3。

---

## 5. 上線路徑：shadow-first（不可跳過）

對齊 [[feedback_env_gated_shadow_verify]]（wire code ≠ 啟用）：

1. **Phase shadow（env 預設 OFF）**：**只在 `under_pressure` 視窗**計算 score + would-be
   路由，但仍跑現有 full path，記「would-be raw 桶 vs 實際 resolved_intent」。
   - 看「壓力下被判 raw、但其實 `resolved_intent=True`」有幾筆（= 會被掉的真意圖）。
   - 同時記 `under_pressure` 一天佔多少時段——若幾乎不發生，整套不值得做（見 §6-A）。
2. **Phase live（過 gate 才開）**：壓力視窗誤殺率達標才讓「不等→raw」真正生效。
3. 持續看 `records/latency_breakdown_<date>.md`：壞日子 p90 應降、wake/點歌成功率不退。

**驗收標準（成功 = 可獨立 loop）**：
- raw-passthrough 誤殺真意圖 < 1%（shadow 一週）
- cleaner full-chain 呼叫量下降可量化（目標先設 −20%）
- 點歌/喚醒端到端成功率不低於現況

---

## 6. 替代方案（office-hours Phase 4）

- **A. 完全不做，靠 PR#20 預算上限**：cleaner 已有 6s 總預算 + 每段 4s。延遲最壞情況已封頂。
  若痛點純是延遲而非 TPD/配額，這個投票的邊際效益可能不值得新增複雜度。
  **先量「痛點是 call 量還是延遲」再決定要不要做這整套。**
- **B. 投票取代 J1 regex judge**：把它做成 IntentBus 的 cheap classifier 而非 cleaner gate。
  但 J1 已存在且 8/8 準（[[judge_race_volume_2026-05-28]]），重疊，否決。
- **C. 純 E3（丟掉 E1/E2 手寫詞）**：直接學全詞表權重。更乾淨但冷啟動無資料時完全不work，
  且失去 E1/E2 的人類可解釋先驗。→ 採折衷：E1/E2 當先驗，E3 當資料修正項（即 §2）。

---

## 7. 待辦（實作時，非本文件範圍）

- [ ] **先量「under_pressure 一天佔幾 %」**：若壞日子很罕見（PR#6 Gemini 2.5 free 池已加
      failover headroom 後），整套不值得做 → 直接收工（§6-A）。這是 go/no-go 前置。
- [ ] `CooldownAwarePool` 加 `all_cooling() -> bool`（quick pool 壓力訊號）
- [ ] 接 downstream `resolved_intent` 回寫 cleaner outcome log（E3 的唯一新接線）
- [ ] `triadic_vote.py` pure core：`score(raw, e3_weights) -> (score, breakdown)`，TDD
- [ ] E2 移除「你」（只留 不/別/停，且僅扣分）
- [ ] env-gated shadow 接線：僅 under_pressure 視窗記 would-be raw vs resolved_intent
- [ ] daily ritual：擬合 E3 + 詞表校準建議（人審）
- [ ] shadow 一週 → 看壓力視窗誤殺率 → 決定是否開 live
