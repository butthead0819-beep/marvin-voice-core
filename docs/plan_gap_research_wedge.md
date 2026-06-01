# Plan：資訊真空偵測 + 靜默交付（最小楔子）

> 範圍：主動型 AI 願景的功能 1（資訊真空偵測）+ 功能 2（非干擾即時交付）。
> 明確**不含**功能 3（async 多 agent 路由）與功能 4（時序因果圖譜）。
> 狀態：plan 定稿，待開工（2026-06-01）。

## 目標

免喚醒偵測對話中的**知識不確定**（疑惑、爭論、資料真空）→ 背景查 → 30 秒內
**靜默**貼到側欄 / Discord 文字頻道，**絕不開口（禁 TTS）**。

## 成功標準（可驗證）

1. 含事實不確定的對話片段 → 產出 research query + 貼答案到側 channel，**無 TTS**
2. 純閒聊無不確定 → **保持沉默**（零誤觸發為目標）
3. Shadow 模式：記錄「會貼什麼」但不貼，env flip 才真交付

## 架構：事件驅動 + 廉價閘門（非輪詢）

關鍵決策：偵測掛在**每句 finalized STT utterance 事件**上（intent gap pipeline 已用此鉤點），
**不是 timer 輪詢**。理由見〈為何不輪詢〉。uncertainty 偵測要跑 LLM，故走背景
（ProactiveAgent/SpeakBus 那條），不進 `bid()`（≤5ms 禁 LLM）。

```
每句 finalized STT utterance（既有事件）
  → 廉價 pre-gate（純規則，無 LLM）：
       · 有疑問/遲疑訊號？（問句、「不知道/到底/是不是」、VAD 停頓）
       · cooldown 過了嗎？（同一波疑惑只查一次）
       · 未命中 → return，零成本
  → gate 通過才跑 UncertaintyDetector（LLM，讀滾動緩衝拿多輪脈絡）
  → 命中 → ResearchAgent.research(query)   [async 背景]
  → SilentDelivery.post(answer)            [CompanionBridge + Discord 文字，禁 TTS]
  → records/gap_research.jsonl（shadow-aware）
```

### 為何不輪詢

「每 15–30s 跑 LLM」= 輪詢，成本 ∝ 掛機時長（120 次/小時，多數回 NONE）。
改事件驅動後成本 ∝「真正疑惑的次數」——閒聊廳掛機沒人發問 = 零 LLM 花費。
pre-gate 因此是**架構本體**，不是事後緩解補丁。

## 元件

| 元件 | 新/既有 | 職責 |
|---|---|---|
| ConversationBuffer | 既有 | 滾動 3 分鐘緩衝，LLM 讀此拿多輪脈絡 |
| STT utterance 鉤點 | 既有 | 觸發源（intent gap 已掛） |
| pre-gate | 新（輕） | LLM 前純規則過濾：疑問/遲疑訊號 + cooldown |
| UncertaintyDetector | 新 | cheap LLM 判有無未解事實疑問 → query 或 NONE；debounce 同疑問 |
| ResearchAgent | 新（v1 可 stub） | 拿 query 查詢；v1 單一來源，介面留 pluggable |
| SilentDelivery | 大半既有 | 走 bridge_emitters 推 CompanionBridge + Discord 文字；**絕不 voice_client.play** |

研究背景脈絡可額外取用 `session_summaries`（derived、不受 14d prune 影響、已有 search/get API）。

## Shadow 上線（按 env-gated 教訓）

- env `GAP_RESEARCH_MODE` = `off | shadow | live`
- shadow：偵測+研究照跑，只寫 jsonl，不交付
- **開機 log 出 mode**，驗 env 真有設（避免 J2 空轉 3 天重演）
- `records/gap_research.jsonl`：`{ts, snippet, query, answer_len, delivered, mode, confidence}` — derived、餵 daily ritual

## 風險

1. **誤觸發洪水**（最大）：閒聊充滿修辭性「不知道」。shadow 先量誤報率，達標才 live。pre-gate 順手擋一部分。
2. **延遲**：tick→研究鏈塞進 30–45s，全程 async 非阻塞。
3. ~~成本~~：事件驅動 + pre-gate 後降級，不再是主要風險。

## TDD 計畫

- `UncertaintyDetector`（注入 mock LLM）：不確定→query / 閒聊→None / 同疑問 debounce
- pre-gate：疑問訊號命中/未命中 / cooldown 行為
- `SilentDelivery`：**斷言永不呼叫 TTS** / 有推 bridge
- mode gating：shadow 記錄不交付 / off 全靜 / live 交付
- 整合 smoke

## 分期

- **Phase 1（shadow）**：pre-gate + detector + 日誌，不交付（研究可 stub），真流量量誤報率 ~1 週
- **Phase 2**：接 research + silent delivery，shadow 達標才 env-flip live
