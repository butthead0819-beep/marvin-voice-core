# 海龜湯 v0 架構文件

實作前的設計筆記。範圍只限 v0 MVP，與 [REQUIREMENTS.md](./REQUIREMENTS.md) 對應。
v1 之後的擴充見 [ROADMAP.md](./ROADMAP.md)。

---

## 目錄

1. [模組地圖](#1-模組地圖)
2. [狀態機](#2-狀態機)
3. [核心資料流](#3-核心資料流玩家語音--marvin-回應)
4. [Engine / LLM 分層](#4-engine--llm-分層為什麼跟-busted99-不一樣)
5. [LLM Judge 設計](#5-llm-judge-設計)
6. [Voice Parse 設計](#6-voice-parse-設計)
7. [Cog 與 Discord 整合](#7-cog-與-discord-整合)
8. [鎖與並發](#8-鎖與並發)
9. [錯誤處理與降級](#9-錯誤處理與降級)
10. [可從 Busted99 直接複製的部分](#10-可從-busted99-直接複製的部分)
11. [v0 刻意不做的設計](#11-v0-刻意不做的設計與替代方案)

---

## 1. 模組地圖

```
game/turtle_soup/
  REQUIREMENTS.md       需求與驗收
  ARCHITECTURE.md       本文件
  ROADMAP.md            v1+ 計畫
  __init__.py
  puzzles.py            v0 hardcode 一題（電梯到 18 樓侏儒題）
  session.py            純 dataclass：TurtleSoupSession / TurtleSoupState / AskedQuestion
  voice_parse.py        STT 文字 → 意圖分類（regex，不走 LLM）
  llm_judge.py          judge_question() + judge_final_guess() + 3-layer fallback
  engine.py             TurtleSoupEngine：state machine + 問答紀錄 + 狀態轉移

cogs/turtle_soup_cog.py
  Discord 介面層；不 import game 內部以外的東西；唯一接 Discord/VoiceController 的地方

tests/test_turtle_soup_*.py
  TDD 測試
```

**唯一外部依賴**（非 game/turtle_soup 內）：
- `game.llm_clients` — 共用 LLM client cache（Cerebras / Groq / Gemini）
- `cogs.voice_controller.VoiceController` — TTS 播放（透過 `self.bot.cogs.get("VoiceController")`）

引擎本身**不 import discord**，跟 Busted99 一樣。

---

## 2. 狀態機

```
IDLE
  │
  │ /turtle_soup_start
  ▼
JOINING ──────── timeout 35s / 立即開始按鈕 ────┐
  │                                              │ 無人加入 → 取消
  ▼                                              │ → IDLE
PRESENTING                                       │
  │ 湯面 TTS 結束（fire-and-forget，不卡 state）│
  ▼                                              │
ASKING ⇄ Q&A loop ──┐                            │
  │                  │                            │
  │                  ├─ voice/text → judge → narration → 留在 ASKING
  │                  │
  │                  ├─ "投降" → end_reason=surrender
  │                  ├─ "答案是 XXX" 通過 final judge → end_reason=win
  │                  ├─ 達 50 題 → 強制觸發 final → end_reason=exhausted
  │                  └─ /turtle_soup_stop → end_reason=cancelled
  ▼
GAME_OVER
  │ 公布湯底（TTS + embed）
  │ 等待用戶清除（v0 無「再來一局」）
  ▼
IDLE（10 分鐘自動回 IDLE 或玩家手動 stop）
```

**重要不變式**：
- 從 ASKING 到 GAME_OVER 的轉移**只能由 engine 觸發**，cog 不直接改 state。
- PRESENTING 不需要等 TTS 結束才進 ASKING——TTS 是 fire-and-forget，state 立刻轉，玩家可以在 Marvin 念湯面時就開始問（這是刻意設計，避免 dead air）。

---

## 3. 核心資料流（玩家語音 → Marvin 回應）

```
玩家在 Discord 語音說話
       │
       ▼
Discord audio sink（既有）─────► VAD（既有）
                                    │
                                    ▼
                                STT（既有：Whisper Swift / Groq / Mac）
                                    │  → transcribed_text
                                    ▼
                       ┌────────────────────────────┐
                       │ cog.receive_voice_question  │  ← STT pipeline 入口
                       │ (speaker, text)             │
                       └────────────┬───────────────┘
                                    │
                       state ≠ ASKING → drop silently
                                    │
                                    ▼
                       voice_parse.classify_intent(text)
                                    │
                  ┌─────────────────┼─────────────────────┐
                  │                 │                     │
              ignore           question              surrender / final_answer
                  │                 │                     │
                  ✗            (FIFO 排隊)              觸發對應流程
                                    │
                                    ▼
                       engine.submit_question(speaker, text)
                                    │
                                    ▼
                       llm_judge.judge_question(...)
                                    │
                                    ▼
                       result = {verdict, narration, _provider}
                                    │
                                    ▼
                       cog.on_judge_result(result) ─┐
                                                    │
                       ┌─── SFX (verdict 對應) ────┤
                       ▼                            │
                       TTS (narration) ─────────────┤
                                                    │
                       embed 更新（顯示問答紀錄）─┘
```

**延遲預算（端對端 P95 ≤ 5s）**：

| 階段 | 預估 | 累計 |
|---|---|---|
| 玩家講完 → VAD 偵測結束 | 0.3-0.8s | 0.8s |
| STT 轉錄 | 0.5-1.5s | 2.3s |
| voice_parse classify | < 0.01s | 2.3s |
| LLM judge call（Cerebras 命中）| 0.4-1.0s | 3.3s |
| TTS 啟動 → 開始播放 | 0.2-0.5s | 3.8s |
| SFX + 短 narration 播放 | 1.0-1.5s | 5.3s |

緊邊界。如果 LLM 走 fallback 多一層，會破 5s。容忍。

---

## 4. Engine / LLM 分層（為什麼跟 Busted99 不一樣）

**Busted99**：有 `Busted99Engine`（code 判定）和 `Busted99LLMEngine(Busted99Engine)`（LLM 判定），覆寫 `submit_guess`。原因是 Busted99 的核心判定（比大小）可以純 code 做，LLM 是 narration 加值。

**海龜湯**：判定本身就是 LLM 的工作（無 code 可取代）。沒有 code-only 版本的意義。所以**只有 `TurtleSoupEngine` 一個類別**，內部直接呼叫 `llm_judge.judge_question()`。

如果未來想加 verified Q&A 鎖（v1 規劃）：在 engine 內加一層 keyword/embedding 比對，命中就用 verified 答案覆寫 LLM 結果。不需要為此分子類。

---

## 5. LLM Judge 設計

### 5.1 Prompt 結構（已在 REPL 校準）

System prompt 重點：
- 三 verdict 嚴格列舉
- Marvin 風格（毒舌簡潔）
- **防洩底鐵律**（這是最重要的約束，REPL 驗證過）
- JSON 輸出 schema

User msg：JSON 含 `湯面 / 湯底 / 歷史問題 / 當前問題`。

完整 prompt 文字直接從 `scripts/turtle_judge_repl.py` 搬過來，**不要重新寫**。已經 8/8 驗證過。

### 5.2 3-Layer Fallback

```
Cerebras (qwen-3-235b)
   │ 失敗（rate limit / timeout / parse error）
   ▼
Groq (llama-3.3-70b)
   │ 失敗
   ▼
Gemini (gemini-2.5-flash)
   │ 失敗
   ▼
return {"verdict": "irrelevant", "narration": "（系統忙線中，請再問一次）"}
```

每層 timeout 5s。三層全掛總計最多 15s——超過驗收 P95 目標。但這是 catastrophic case，0.1% 機率。

### 5.3 Final Guess Judge（次要 prompt）

獨立的 prompt，input 多一個 `key_facts` 陣列：

```python
PUZZLE = {
    "surface": "...",
    "truth": "...",
    "key_facts": [
        "男子是侏儒（或身材矮小）",
        "電梯按鈕的高度問題",
        "他構不到 22 樓按鈕",
        "18 樓是他能按到的最高樓層",
        "有人陪同搭電梯時可以直達 22 樓",
    ],
}
```

LLM 比對玩家答案 vs `key_facts`，回 `covered_facts: [index]` 列表。

**判定接受門檻**：覆蓋 `key_facts[0]` 與 `key_facts[1]` **兩個核心事實**算接受（侏儒身分 + 按鈕高度問題）。其他三個是 bonus，但不強制。

為什麼門檻不是「全部命中」？因為玩家口述會省略次要細節，要求全 cover 太嚴格。實務上抓住主因即可。

### 5.4 反幻覺保險（v0 簡化版）

v0 不做 verified Q&A 鎖。改用**後處理 keyword 過濾**：

```python
LEAK_KEYWORDS = ["侏儒", "矮", "身材", "按鈕", "夠不到", "構不著", "按不到"]

def post_filter_narration(narration: str, question: str) -> str:
    """若 narration 含洩底詞，但問題本身不含 → 改寫為通用回應。"""
    for kw in LEAK_KEYWORDS:
        if kw in narration and kw not in question:
            return "（這個方向有點意思）" if last_verdict == "yes" else "（再想想）"
    return narration
```

這是**第二層防線**，prompt 是第一層。REPL 顯示 prompt 已經能擋住 95%+ 洩底，後處理是兜底。

---

## 6. Voice Parse 設計

純 regex，不走 LLM。因為 v0 流量會比 Busted99 高（每場 20-50 個問句 vs Busted99 一場 5-10 個猜題）。

```python
INTENT_PATTERNS = {
    "surrender": [r"投降", r"不玩了", r"放棄", r"我認輸", r"我棄權"],
    "final_answer": [r"^答案是", r"^我認為答案是", r"^我覺得是", r"^我猜是"],
}

IGNORE_FILTERS = [
    lambda t: len(t) < 4,  # 太短
    lambda t: t in {"嗯", "啊", "對啊", "好", "OK", "ok", "嗯啊"},  # 純語助詞
]

def classify_intent(text: str) -> dict:
    text = text.strip()
    for filt in IGNORE_FILTERS:
        if filt(text):
            return {"intent": "ignore", "payload": text}
    for intent, patterns in INTENT_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, text):
                payload = text
                if intent == "final_answer":
                    payload = re.sub(r"^(答案是|我認為答案是|我覺得是|我猜是)", "", text).strip()
                return {"intent": intent, "payload": payload}
    return {"intent": "question", "payload": text}
```

**設計理由**：STT 對贅字會轉錄出來，但贅字後面有實質內容才需要送 judge。一句「嗯啊」純粹是語助詞，不送 LLM 省成本。

**「請問」前綴 gate（v0.2 新增）**：

實測 v0 發現玩家邊討論邊推理，自然句子（「他是侏儒嗎？」「我覺得是身高吧」）會塞滿 LLM judge，雜訊高、成本高、SFX/TTS 一直響很煩。

加入 `_QUESTION_PREFIXES = ["我可以問", "我想問", "問題是", "問一下", "我問你", "問你", "我問", "請問"]`（按長度降序避免 prefix 互吃）。沒前綴的句子歸 `discussion` intent，cog 收到後 return False，直接丟棄。

```python
async def receive_voice_answer_by_speaker(self, speaker, text):
    ...
    intent_result = classify_intent(text)
    intent = intent_result["intent"]
    if intent == "discussion":
        logger.debug("[TurtleSoup] discussion ignored: %r", text[:60])
        return False  # 完全靜默
    ...
```

Embed footer 與 Marvin 念完湯面後的 TTS 都會明確提醒「請用『請問』開頭發問」。

**Hint 系統（v0.3 新增）**：

兩條觸發路徑共用同一個 `_handle_hint_request(source)`：
- **玩家主動**：「請問給我一個提示」/ 「請問可以給線索嗎」→ voice_parse 抓 hint_request intent → cog dispatch
- **idle timer**：進 ASKING 後 `asyncio.sleep(60)`，60s 內任何 user 活動會 cancel & restart timer；timer 跑完且仍在 ASKING → auto 觸發

```
engine.request_hint() → 從 puzzle.hints[session.hints_given] 取，並 +1
                     → 用完回 None
cog._handle_hint_request(source="player" | "idle")
  ├─ hint != None：播 fanfare SFX → TTS「提示 N：...」→ 貼 embed
  │                 → 重啟 idle timer
  └─ hint == None：
       source="player" → 回「提示已經給完，剩下的自己想吧」
       source="idle"   → 靜默（不打擾，避免循環敲門）
```

Hint 不消耗 max_questions 配額（v0 沒計分，所以 hint 是免費的）。v3 加計分後 hint 會有分數懲罰。

**idle timer 生命週期**：
- `on_state_change(ASKING)` 啟動 timer
- 每次 question / hint_request / surrender / final_answer 都 reset
- `on_state_change(GAME_OVER)` 取消 timer
- `_cancel_tasks()` 也會連帶取消（透過 `_idle_hint_task` 引用）

**Hint 編織網（v0.5 graph 模型）**：

把 hint 從線性 list 升級成「節點 + 揭露關係」的網。核心觀念：

- **HintNode**：atomic insight（推理鏈中的一環，例如「身體有不尋常的限制」）
- **Hint**：提示文字 + 它揭露哪些節點（`reveals: tuple[str, ...]`）
- **共用 ABC**：同一節點可被多條 hint 共用；hint 不是獨立小卡片，是節點子集的可視化
- **單調遞進**：後一條 hint 的 reveals **必須包含**前一條（不撤回，只延伸）

```python
@dataclass(frozen=True)
class HintNode:
    id: str       # 'body_limit'
    fact: str     # 「主角身體有不尋常的限制」

@dataclass(frozen=True)
class Hint:
    text: str
    reveals: tuple[str, ...] = ()  # ['body_limit'] / ['body_limit', 'button_reach']
```

**ELEVATOR_18F 推理網**：
```
body_limit ────► button_reach ────► assist_dependence
(身體限制)      (按鈕觸及範圍)    (依賴別人幫忙)
```

對應 hints：

| Hint | reveals | 可視化（vs 3 個節點） |
|---|---|---|
| 「想想他身體上的限制會怎麼影響日常動作」 | `('body_limit',)` | `|■··|` |
| 「為什麼他能下到 1 樓卻上不了 22 樓？這差在哪？」 | `('body_limit', 'button_reach')` | `|■■·|` |
| 「有別人一起時能到頂樓，自己卻不行 — 這依賴什麼條件？」 | `('body_limit', 'button_reach', 'assist_dependence')` | `|■■■|` |

**Top-down 抽節點 + Bottom-up 組提示（LLM 工作流）**：

```
階段 1（top-down）：從湯底逆推
    LLM 讀 truth → 抽出 3-5 個 atomic insight 節點
    例：body_limit → button_reach → assist_dep

階段 2（bottom-up）：用節點當積木組 hints
    LLM 用節點子集組合 3 條 hint
    每條 hint.reveals 必須 ⊇ 前一條
    每條至少多揭露一個新節點（嚴格遞進）

兩階段在一次 LLM call 內完成（JSON 輸出兩個 array）。
```

**`generate_hint_graph()` validation 不變式**（在 `_validate()` 鎖死）：

1. ≥ 2 個 nodes，≥ 2 條 hints
2. 沒有重複的 node id
3. 每個 hint.reveals 引用的 id 都在 hint_nodes 裡定義過
4. 後一條 reveals ⊇ 前一條（monotonic）
5. 嚴格遞進：每條都必須揭露至少一個新節點（current != prev）

違反任何條件 → 該 LLM 回應視為失敗，try 下一層 fallback。

**後處理 `_filter_leaks`**：
- hint.text 含 leak_keywords → 加 `⚠[LEAK:KW]` 標記
- hint_nodes.fact 是內部欄位（玩家看不到）→ 不過濾
- 不自動改寫，讓作者親自決定保留 / 重生 / 改寫

**作者工作流**：
```bash
# 在 puzzles.py 寫好 Puzzle 骨架（surface / truth / key_facts / leak_keywords）
# 跑 generator
python scripts/generate_puzzle_hints.py NEW_PUZZLE_ID -n 3

# CLI 印出：
#   - hint_nodes 清單（含 id + fact）
#   - hints 清單（含 text + reveals + 視覺化 |■■·|）
#   - 可直接貼回 puzzles.py 的 paste block

# 作者挑選 / 混合 / 改寫 → 貼回 puzzles.py
```

**為什麼不在 runtime 動態生成？**
v0/v1 採離線生成因為：
- 品質控制（作者可選 / 改寫）
- Deterministic gameplay（同題玩家拿到一樣的 hints）
- 0 runtime LLM 成本

v4（UGC 階段）會考慮 lazy 生成 — 玩家投稿題目時自動生 graph + 寫入 pending pool 等審核。

**v1 升級已完成（個人化 hint 排序 + 分支 + 非相鄰 reveals）**：

### 1. HintNode.keywords — 玩家問題 → 節點映射

```python
@dataclass(frozen=True)
class HintNode:
    id: str
    fact: str
    keywords: tuple[str, ...] = ()  # 玩家問題含這些詞 → 視為已探索此節點
```

ELEVATOR_18F 範例：
```python
HintNode(
    id="body_limit",
    fact="男子身體有不尋常的限制",
    keywords=("身高", "身材", "身體", "矮", "侏儒", "個子", "高度"),
)
```

玩家問「他身高有問題嗎？」→ 命中 `身高` → `body_limit` 被標記已探索 → 之後 hint 排序會跳過只揭露 `body_limit` 的 hint。

### 2. Engine.`_select_next_hint_index()` — 資訊增益選法

```python
def _select_next_hint_index(self) -> Optional[int]:
    explored = self._explored_node_ids()  # given_hints + question keywords 命中
    given = set(self.session.given_hint_indices)

    candidates = []
    for i, hint in enumerate(self.puzzle.hints):
        if i in given:
            continue
        new_nodes = set(hint.reveals) - explored
        if not new_nodes:
            continue  # 沒新內容，跳過
        candidates.append((len(new_nodes), len(hint.reveals), i))

    candidates.sort()  # asc: 小 new_nodes 優先，同數量選小 reveals
    return candidates[0][2] if candidates else None
```

**排序語義**：
- 主鍵 `len(new_nodes)` 升序 — 越循序漸進越好（給 1 個新節點 > 給 3 個）
- 次鍵 `len(reveals)` 升序 — 同 info gain 下選最乾淨的 hint
- 末鍵 list 順序 — 作者預設先後當 tie-breaker

**Linear puzzle**（ELEVATOR_18F）行為不變：
- 第 1 次 request → new={A}, reveals=1 → hint[0]
- 第 2 次 → A 已探索 → new={B}, reveals=2 → hint[1]
- 第 3 次 → A,B 已探索 → new={C}, reveals=3 → hint[2]

**Branch puzzle** 行為（合成測試用 `BRANCH_PUZZLE`）：
- hints=[hint_x(x,), hint_y(y,), hint_z(z,), hint_xyz(x,y,z)]
- 第 1 次 → 三個 1-node hint 都 (1, 1)，選列表最前的 hint_x
- 第 2 次 → x 已探索 → hint_y / hint_z 都 (1, 1)、hint_xyz (2, 3) → 選 hint_y
- 第 3 次 → hint_z
- 第 4 次 → 所有節點探索完 → hint_xyz 沒新內容 → 回 None

**Question-driven** 行為：
- 玩家問了「他身高有問題嗎？」（命中 `身高` keyword）→ `body_limit` 已探索
- 第 1 次 request_hint → 跳過 hint[0]（沒新節點）→ 直接給 hint[1] 揭露 `button_reach`

### 3. Session.given_hint_indices

```python
@dataclass
class TurtleSoupSession:
    ...
    given_hint_indices: list[int] = field(default_factory=list)
```

記錄已給的 hint 在 `puzzle.hints` 的 index，避免重複給 + 計算 explored。

### 4. 何時引擎仍回 None
- ASKING state 外
- 所有 hint 對玩家「沒新資訊」（given + explored 已覆蓋全部 reveals）

### 未來擴充（v2+）
- LLM-based 問題理解（不只 keyword 匹配；用 embedding / 小 LLM call）
- 玩家「rating」hint（讓玩家對 hint 評分，下次選相似 hint）
- 多 puzzle 並行時跨題學習 hint 風格

---

## 7. Cog 與 Discord 整合

### 7.1 結構複用 Busted99

```python
class TurtleSoupCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._engine: Optional[TurtleSoupEngine] = None
        self._session: Optional[TurtleSoupSession] = None
        self._channel: Optional[discord.TextChannel] = None
        self._tasks: set[asyncio.Task] = set()
        self._name_to_id: dict[str, int] = {}
        self._asking_inflight = 0  # FIFO judge 排隊計數
        self._MAX_ASKING_INFLIGHT = 3  # 對齊 STT inflight cap
```

### 7.2 Slash Commands

| 指令 | 行為 |
|---|---|
| `/turtle_soup_start` | 建 session，進入 JOINING（限制：同頻道一次只能一場） |
| `/turtle_soup_stop` | 強制結束（任何 state） |
| `/turtle_soup_show` | 重看湯面（任何 state，方便玩家翻 chat） |

### 7.3 STT Hook

於 STT pipeline 加入 dispatch：

```python
# 在 dispatch_transcription（既有）內：
turtle_cog = bot.cogs.get("TurtleSoupCog")
if turtle_cog and turtle_cog.is_active():
    await turtle_cog.receive_voice_question_by_speaker(speaker, text)
```

`is_active()` 判斷：state in {JOINING, PRESENTING, ASKING}。GAME_OVER / IDLE 不接收。

### 7.4 Game Mode 切換

進入 ASKING 時 `vc.game_mode = True`（沿 Busted99）。離開時 `False`。原因：game_mode 會降低 conv_buffer 上限、繞過 silence gate，確保 Marvin 主持有優先權。

### 7.5 SFX / TTS 序列

完全沿 Busted99 SFX→TTS chain pattern（剛在這個 session 寫過的）：

```python
async def _judge_chain(self, vc, verdict: str, narration: str):
    sfx_name = {"yes": "correct", "no": "buzz", "irrelevant": "ba_dum_tss"}[verdict]
    await self._play_sfx(sfx_name)
    await self._fire_tts(vc, narration)

# 在 receive_voice_question_by_speaker 內：
self._spawn(self._judge_chain(vc, verdict, narration))
```

---

## 8. 鎖與並發

### 8.1 為什麼需要 inflight cap

多人同時問問題時，judge LLM call 會排隊。沒有 cap 會：
- LLM provider rate limit 觸發機率上升
- 延遲累積（5 個排隊 = 25s 等待）
- 玩家體驗：「我問了 Marvin 都不回」

`_MAX_ASKING_INFLIGHT = 3`：同時最多 3 個 LLM judge 進行中，超過則該問題回 channel「Marvin 還在想上一題，請稍等」，**不送 LLM**。

### 8.2 為什麼不用 Lock

不用 `asyncio.Lock` 序列化所有 judge，是因為：
- 兩個玩家問了不同問題，平行 judge 沒有衝突（state 只在 judge 結束時 append 到 history）
- Lock 會強制序列化，延遲翻倍

只用 counter + cap 即可。append history 用 `asyncio.Lock` 保護（短臨界區，幾乎不阻塞）。

### 8.3 與 Busted99 的差異

Busted99 一次只有一個 guesser 在猜，所以是天然序列化。海龜湯任何人都能問，天然並發。Cap 是必要的安全閥。

---

## 9. 錯誤處理與降級

| 故障點 | 行為 |
|---|---|
| STT 轉錄失敗 | sink 已有 fallback，cog 收到空字串 → 不觸發 judge |
| voice_parse 把 question 誤判為 ignore | 玩家會再說一次，無嚴重後果 |
| 第一層 LLM (Cerebras) timeout / rate limit | 自動 fallback Groq，玩家無感 |
| 三層 LLM 全掛 | 回傳 `{"verdict": "irrelevant", "narration": "（系統忙線，請再問一次）"}`，遊戲不中斷 |
| TTS 失敗 | embed 已含文字 verdict 與 narration，玩家用看的；遊戲繼續 |
| 玩家在 PRESENTING 時就開始問 | 接收但排隊；ASKING 開始後逐一 judge |
| `_MAX_ASKING_INFLIGHT` 滿 | channel.send「Marvin 還在想，請稍等」，question 丟棄不入 history |
| 玩家斷線 | 不偵測，遊戲繼續；剩 0 人時 1 分鐘自動 stop |
| Discord 自身 outage | 全 cog 失效，無對策 |

---

## 10. 可從 Busted99 直接複製的部分

| 機制 | Busted99 來源 | 複製成本 |
|---|---|---|
| Slash command + JOINING + 「立即開始」按鈕 | `busted99_cog.py:73-110` | 改名稱 |
| `_play_sfx` + SFX→TTS chain coroutine | 本 session 剛寫的 [busted99_cog.py:1163-1192](../../cogs/busted99_cog.py#L1163) | 改 SFX name mapping |
| 3-layer LLM fallback（client cache + try chain）| `busted99/llm_engine.py:215-253` | 直接搬，改 prompt |
| Game mode 切換（`vc.game_mode = True`）| `busted99_cog.py:1226-1230` | 直接搬 |
| `_send_player_links` 個人連結（v1 才用，v0 跳）| `busted99_cog.py:698-722` | v0 不抄 |
| WS broadcast（v1 才用）| `busted99_cog.py:577-638` | v0 不抄 |
| `should_suppress_for_game_by_id` STT 早期過濾 | `busted99_cog.py:1021-1031` | 改成「state in ASKING 才接」 |
| 測試模板（async + asyncio_mode strict + mock）| `tests/test_busted99_*.py` | 直接抄 fixture |

**估算複用率：60%**。新寫的主要是 LLM judge prompt + voice_parse + 整體 state machine 縮減。

---

## 11. v0 刻意不做的設計與替代方案

| 不做 | 為什麼 | v0 替代 |
|---|---|---|
| Verified Q&A 鎖 | 需要題目作者手動標註，v0 只有 1 題 hardcode 不需要 | post-filter keyword 黑名單 |
| 多題庫 | 1 題就能驗證假設 | hardcode in `puzzles.py` |
| Hint 系統 | 不影響核心 loop 驗證 | 玩家自己想，卡住可投降 |
| Dispute 機制 | 需要 prompt 改寫流程 + 取信機制，太複雜 | 玩家不爽就投降下一場 |
| 計分 | 不影響核心 loop | 結束畫面顯示「總提問數」就好 |
| Web UI | Discord 內已足夠驗證 | embed + voice |
| Companion bridge | 與驗證假設無關 | 不接 bridge emit |
| LLM 抽取意圖（取代 regex voice_parse）| 多一次 LLM call 增加延遲與成本 | regex 已能 cover 95% case |
| 5-verdict（close, important）| 增加 prompt 複雜度，REPL 顯示 3-verdict 已堪用 | 玩家自己判斷 narration 的 hint |
| Marvin 當玩家 | 多寫一個 marvin_player.py，與假設無關 | 純人類玩家 |

---

## 12. 實作順序建議

按依賴從低到高：

1. **`puzzles.py`** — 1 題 hardcode 寫死 `PUZZLE = {...}`（5 分鐘）
2. **`session.py`** — dataclass，4 state，AskedQuestion record（15 分鐘）
3. **`voice_parse.py`** + 測試 — 純函式，TDD（30 分鐘）
4. **`llm_judge.py`** + 測試 — judge_question / judge_final_guess + 3-layer fallback，mock provider（1 小時）
5. **`engine.py`** + 測試 — state machine + submit_question + final_guess + surrender（2 小時）
6. **`cogs/turtle_soup_cog.py`** — 從 busted99_cog 改造，state dispatch + STT hook + UI（2 小時）
7. **整合測試** — 跑一場端對端（30 分鐘）
8. **真實 Discord 上線測試** — 對驗收標準 A1-A10 逐項打勾（1 小時）

**總工時估算：7-8 小時**（不含意外與調整）。

---

## 13. 已知未解問題（實作中可能跑出來）

1. **STT 對「答案是侏儒因為他按不到 22 樓按鈕」這種長句子的轉錄延遲與正確率**——需要實測才知道。
2. **`asked_history` 取最近 10 個是否夠**——可能玩家會重複問相同問題（STT 變體），需要去重或語意比對。
3. **TTS 念湯面 80 字大約多久**——若 > 8 秒會悶。可能需要切段播放或壓縮湯面文字。
4. **多人並發問同樣意思的問題**——目前是各自 judge，可能 Marvin 連續回兩次「沒錯」，浪費。或許需要 dedup 機制（但會增加延遲）。

這些問題不在 v0 預先解，實作 + 上線測試後依實際情況決定。
