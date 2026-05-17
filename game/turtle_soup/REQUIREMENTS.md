# 海龜湯 v0 MVP — 需求與驗收文件

文件版本：v0.1（2026-05-17）
階段：MVP（最小可玩驗證）
類比：Busted99 之於「終極密碼」，海龜湯 v0 之於「LLM-judged Q&A 遊戲」

---

## 1. 目標與單一驗證假設

**驗證假設**：「玩家用語音自由問是非題 → STT 轉錄 → LLM 判定 yes/no/irrelevant」這條 loop 在 Discord 多人語音場景下，能撐起一場「讓人覺得 Marvin 真的在主持遊戲」的體驗。

只驗證這一件事。其他都是次要。

**成功定義**：
- 一場遊戲（含 5+ 玩家問題、1 次猜中或投降）能無中斷跑完
- 玩家口語問句的 verdict 正確率 ≥ 80%
- 0 次洩底（narration 含湯底機制關鍵詞）
- 整輪 Q&A 延遲（玩家講完 → Marvin 講完）≤ 5 秒 P95

不達標就停下檢討，不直接堆功能。

---

## 2. 範圍

### 2.1 做（v0 in scope）

- 1 題 **hardcode 在 code 裡** 的種子湯（電梯到 18 樓侏儒題）
- 4 階段 state machine：`IDLE → JOINING → PRESENTING → ASKING → GAME_OVER`
- LLM judge 3 verdict：`yes` / `no` / `irrelevant`
- 玩家自由問是非題（語音 / 鍵盤皆可）
- 「投降」結束：玩家喊「我投降」/「不玩了」→ Marvin 公布湯底
- 「最終猜答」結束：玩家喊「答案是 ...」格式 → LLM 判定接受 / 駁回
- 50 題硬上限（接近時 Marvin 主動提示「再 N 題就強制結束」）
- SFX 提示三段：yes / no / irrelevant 各一個音效（沿用 Busted99 模式）
- TTS：Marvin 念湯面、回 verdict narration、公布湯底
- Discord cog 整合（slash command 啟動、嵌入式 embed 顯示題目）
- TDD：所有狀態機行為與 LLM judge 介面都有測試

### 2.2 不做（v0 out of scope，後階段再說）

- **不做** 題庫管理（JSON bank、難度標籤、抽題隨機化）
- **不做** 多題庫切換
- **不做** Hint / Clue 系統（玩家卡住主動給線索）
- **不做** Dispute 機制（玩家質疑 Marvin 判定）
- **不做** Verified Q&A 鎖（題目作者預先標註的問答對覆蓋 LLM 判定）
- **不做** 計分系統
- **不做** 排行榜 / 成就 / 統計
- **不做** 多人 turn-taking（任何時刻任何人都能問，FIFO 處理）
- **不做** Marvin 主動參與（不當猜題者）
- **不做** 5-verdict 細分（close / important）
- **不做** Web UI / Companion bridge 整合
- **不做** Twitch chat 投票機制

**判斷新需求是否該砍**：問「不做這個，能不能驗證單一驗證假設？」能 → 砍。

---

## 3. 玩法流程

### 3.1 啟動

玩家輸入 `/turtle_soup_start` → cog 建 session、進入 JOINING。

### 3.2 JOINING（≤ 35 秒）

- Discord embed 顯示「等待玩家加入」+ Join 按鈕
- 35 秒後或玩家按「立即開始」→ 進入 PRESENTING
- 至少需 1 位人類玩家

### 3.3 PRESENTING（湯面播放）

- Marvin TTS 念出湯面文字
- 同時 embed 顯示湯面（玩家可隨時重看）
- TTS 結束 → 自動進入 ASKING

### 3.4 ASKING（核心循環，最多 50 題）

- embed 顯示：當前題數 / 50、上一個 verdict、剩餘提問次數
- 玩家可：
  - 語音問是非題（STT → LLM judge → SFX + TTS narration）
  - 鍵盤輸入問題（直接 LLM judge → SFX + TTS narration）
  - 語音 / 鍵盤喊「我投降」/「不玩了」/「放棄」→ 結束（投降流程）
  - 語音 / 鍵盤以「答案是 XXX」/「我認為 XXX」格式給最終答案 → 最終猜答流程
- 達 50 題 → Marvin 提示「最後 5 題」→ 達 50 → 自動觸發「最終猜答」階段

### 3.5 最終猜答（FINAL_GUESSING，最多 30 秒）

> 不另設 state，仍歸在 ASKING 內，但 LLM 用不同 prompt 判定。

- 玩家口述完整答案
- LLM judge 用 final-guess prompt 比對玩家答案 vs 湯底
- 接受 → GAME_OVER（勝利路徑）
- 駁回 → 回到 ASKING（剩餘提問次數不變）

### 3.6 GAME_OVER

- 三種結束原因：win（猜中）/ surrender（投降）/ exhausted（題數用完未猜中）
- Marvin TTS 公布湯底全文
- embed 顯示湯底 + 玩家總提問次數 + 結束原因
- 「再來一局」按鈕（v0 因為只有一題，按了會顯示「敬請期待更多題目」）

---

## 4. State Machine

```
IDLE
  │ /turtle_soup_start
  ▼
JOINING ─────────────────────────────┐
  │ timeout 35s or 立即開始按鈕      │
  ▼                                  │ 無人加入 → 取消
PRESENTING                           │
  │ TTS 完成                         │
  ▼                                  │
ASKING ⇄ (judge loop)                │
  │ ├─ 投降                          │
  │ ├─ 最終猜答正確                  │
  │ └─ 達 50 題且最終猜答失敗        │
  ▼                                  │
GAME_OVER ◄──────────────────────────┘
  │ 「再來一局」按鈕（v0 不啟用）
  ▼
IDLE
```

---

## 5. 模組分工

對齊 Busted99 結構：

```
game/turtle_soup/
  REQUIREMENTS.md       本文件
  ARCHITECTURE.md       詳細實作架構（之後寫）
  __init__.py
  session.py            TurtleSoupSession, TurtleSoupState, AskedQuestion
  engine.py             TurtleSoupEngine：流程控制、Q&A 紀錄、狀態轉移
  llm_judge.py          judge() + final_guess_judge() + 共用 3-layer fallback
  voice_parse.py        STT 文字 → 意圖分類（question / surrender / final_answer）
  puzzles.py            v0 hardcode 一題；後續可擴成 bank loader

cogs/turtle_soup_cog.py  Discord 介面 + state dispatch + SFX/TTS 串接

tests/test_turtle_soup_*.py  TDD 測試
```

---

## 6. 介面契約

### 6.1 LLM Judge

```python
async def judge_question(
    surface: str,         # 湯面（玩家可見）
    truth: str,           # 湯底（玩家不可見）
    question: str,        # 玩家當前問題
    asked_history: list[str],  # 已問過的問題（取最近 10 個進 prompt）
) -> dict:
    """
    回傳 {
      "verdict": "yes" | "no" | "irrelevant",
      "narration": "<10-25 字 Marvin 風格回應>",
      "_provider": "Cerebras" | "Groq" | "Gemini" | "fallback",
    }
    """
```

System prompt 約束已通過 REPL 校準（見 `scripts/turtle_judge_repl.py`）。

### 6.2 LLM Final Guess Judge

```python
async def judge_final_guess(
    surface: str,
    truth: str,
    key_facts: list[str],  # 湯底關鍵點清單（玩家答案需 cover ≥ 60% 才算對）
    player_answer: str,    # 玩家口述的完整答案
) -> dict:
    """
    回傳 {
      "accepted": bool,
      "covered_facts": list[int],   # 命中 key_facts 的 index
      "narration": "<Marvin 對玩家答案的評語>",
      "_provider": str,
    }
    """
```

### 6.3 Voice Parse

```python
def classify_intent(text: str) -> dict:
    """
    語音轉文字 → 意圖分類，純 regex / keyword，不走 LLM。
    
    回傳 {
      "intent": "question" | "surrender" | "final_answer" | "ignore",
      "payload": str,   # 對 question 是原文；對 final_answer 是擷取後的答案部分
    }
    """
```

判斷規則：
- `surrender`：含「投降」/「不玩了」/「放棄」/「我認輸」
- `final_answer`：開頭含「答案是」/「我認為答案是」/「我覺得是」
- `ignore`：< 4 字、或全是語助詞「嗯」「啊」「對啊」
- 其他：`question`

### 6.4 SFX 對應

| Verdict | SFX | 時長 | 用途 |
|---|---|---|---|
| yes | `correct.wav`（既有）| ~1s | 肯定鈴聲 |
| no | `buzz.wav`（既有）| ~0.4s | 否定低鳴 |
| irrelevant | `ba_dum_tss.wav`（本 session 新增）| ~1.3s | 鼓點 + 鈸（不痛不癢的「啊？」感）|
| 投降 / 題數用完 | `sad_horn.wav`（既有）| ~3s | 失敗號角 |
| 猜中勝利 | `fanfare.wav`（既有）| ~2s | 勝利號角 |

序列原則沿 Busted99：SFX → TTS 序列播放，同一個 spawned task。

---

## 7. 非功能要求

| 項目 | v0 目標 | 度量方式 |
|---|---|---|
| 端對端延遲（玩家講完 → Marvin 講完 verdict）| P95 ≤ 5s | 手動計時 10 輪取 P95 |
| LLM judge 正確率 | ≥ 80%（人工標註基準） | 跑 20 個校準問題人工檢視 |
| 洩底率 | 0%（人工檢視 20 輪問答無 narration 含湯底關鍵詞）| 人工檢視 |
| LLM provider fallback | Cerebras → Groq → Gemini 三層皆可獨立運作 | 各層單元測試 mock + 整合測試 |
| STT 失敗或 LLM 全層掛 | 不 crash；玩家收到「請再試一次」提示 | 故障注入測試 |
| 單次遊戲成本 | LLM 部分 ≤ NT$1（約 50 個 judge call）| 跑完一場後抓 token usage 估算 |

---

## 8. 驗收標準（Acceptance Criteria）

每條都要 pass 才算 v0 完成。

### A1：完整單場流程
- [ ] `/turtle_soup_start` 啟動，玩家加入，湯面播放，問題接收，最終結束（任一方式）
- [ ] 全程沒有未捕捉的 exception
- [ ] embed UI 在每個 state 顯示對應內容

### A2：LLM Judge 邏輯正確性
- [ ] 用 20 個校準問題（位於 `tests/turtle_soup_calibration_questions.json`，由人工標註 expected verdict）跑判定
- [ ] verdict 正確率 ≥ 80%（16/20）
- [ ] narration 0 含湯底機制關鍵詞（侏儒、夠不到、按鈕、身高、構不著）

### A3：語音輸入端對端
- [ ] 玩家用語音問「他是侏儒嗎？」→ STT 轉錄 → LLM judge → Marvin TTS「沒錯」之類回應，全程 ≤ 5s
- [ ] 語音問「我投降」→ 觸發投降流程
- [ ] 語音問「答案是因為他是侏儒按不到按鈕」→ 觸發最終猜答流程

### A4：3-layer LLM Fallback
- [ ] 單元測試覆蓋 Cerebras 失敗 → Groq 接手
- [ ] 單元測試覆蓋 Cerebras + Groq 失敗 → Gemini 接手
- [ ] 單元測試覆蓋三層全掛 → 回傳 fallback 文字 + verdict="irrelevant"

### A5：State 防呆
- [ ] 非 ASKING 狀態收到語音問題 → 靜默忽略，不送 LLM
- [ ] 同一玩家連續問 5 個「啊」「嗯」 → 全部被 voice_parse 過濾，不送 LLM
- [ ] 玩家斷線 → 遊戲繼續進行（直到剩 0 人才強制結束）

### A6：SFX / TTS 序列
- [ ] yes verdict → correct SFX 播完 → TTS narration 播完，無重疊
- [ ] no verdict → buzz SFX → TTS narration 序列
- [ ] irrelevant verdict → ba_dum_tss SFX → TTS narration 序列

### A7：Hardcode 題目播放正確
- [ ] PRESENTING 階段 Marvin TTS 念出完整湯面文字
- [ ] GAME_OVER 階段 Marvin TTS 念出完整湯底文字

### A8：測試覆蓋
- [ ] `tests/test_turtle_soup_engine.py` 覆蓋 state machine 所有轉移
- [ ] `tests/test_turtle_soup_judge.py` 覆蓋 judge fallback 與洩底封口檢查
- [ ] `tests/test_turtle_soup_voice_parse.py` 覆蓋意圖分類三類別 + ignore
- [ ] `tests/test_turtle_soup_cog.py` 覆蓋 cog state dispatch 與 SFX 觸發
- [ ] `pytest tests/test_turtle_soup_*` 全綠
- [ ] 既有 `pytest tests/` 不被新測試破壞

### A9：成本與性能
- [ ] 跑一場完整遊戲（20 個問題），總 LLM token cost 透過 provider dashboard 抓取，記入 `game/turtle_soup/ARCHITECTURE.md`
- [ ] 同一場遊戲 P95 端對端延遲 ≤ 5s

### A10：洩底鐵律
- [ ] 人工跑 20 輪問答後，narration 0 次出現以下湯底關鍵詞：「侏儒」「身高」「夠不到」「構不著」「按鈕」「按不到」「矮」「身材」
- [ ] 即使玩家直接問「他是侏儒嗎？」也只回「沒錯」「你抓到了」之類，不複述「侏儒」一詞

---

## 9. 已知風險與應對

| 風險 | 機率 | 影響 | 應對 |
|---|---|---|---|
| STT 對口語贅字（啊、嗯、那個）轉錄成有意義文字，誤觸發 judge | 中 | 中 | voice_parse 過濾短句 + 全是語助詞的句子 |
| LLM 把「答案是 XXX」誤判為一般問題，未進最終猜答 | 中 | 中 | voice_parse 先 regex 擷取「答案是」開頭，cog 層分流 |
| 多人同時問問題，judge 排隊堵塞 | 低 | 低 | FIFO 序列處理（沿 Busted99 inflight cap 模式） |
| LLM 偶發洩底（特定問法繞過 prompt 約束）| 中 | 高 | A10 人工檢視 + 後續加 keyword 黑名單後處理 |
| 玩家持續問亂七八糟、永遠不收尾 | 中 | 低 | 50 題硬上限 + 接近時 Marvin 主動提示 |
| Cerebras 高峰時 rate limit 拖延延遲 | 高 | 中 | 已驗證 fallback 機制，Groq 接手 transparent |
| TTS 念湯面太長（湯底 80 字 / 湯面 100 字）超時 | 低 | 中 | 強制 macOS say 走本機（沿 Busted99 force_macos=True） |

---

## 10. 開放問題（v0 後再決定）

- 多題庫格式：JSON / SQLite / Markdown？
- 題目難度標籤怎麼定義？
- 玩家可以「跳過此題、換一題」嗎？
- Marvin 自己玩（補位猜題）的人格設計？
- Twitch chat 觀眾投票影響 verdict？
- 連續多場「主題模式」（例如：本週都是恐怖題、本週都是溫馨題）？

---

## 11. 驗收流程

1. 所有 A1-A10 標記完成
2. 跑 `pytest tests/ -k turtle_soup` 全綠
3. 上線一場真實 Discord 遊戲，全程錄影
4. 錄影中對照 A2 / A6 / A10 人工標註
5. 通過 → 寫 v0 ship 心得到 `game/turtle_soup/ARCHITECTURE.md`，公開該模組進入維護期
6. 未通過 → 列具體 fail case，回到設計階段
