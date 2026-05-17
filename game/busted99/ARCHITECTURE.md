# Busted99 架構文件

給下一個遊戲看的設計筆記。覆蓋「為什麼這樣設計」、「踩過哪些雷」、「可以直接複製哪些部分」。

---

## 目錄

1. [模組地圖](#1-模組地圖)
2. [狀態機](#2-狀態機)
3. [分數設計](#3-分數設計反直覺是核心)
4. [Engine / LLMEngine 繼承分層](#4-engine--llmengine-繼承分層)
5. [鎖與並發](#5-鎖與並發)
6. [DB 設計與 threading](#6-db-設計與-threading)
7. [語音整合](#7-語音整合)
8. [Web UI（WebSocket state）](#8-web-uiwebsocket-state)
9. [踩到的雷](#9-踩到的雷)
10. [下一個遊戲可以複製什麼](#10-下一個遊戲可以複製什麼)

---

## 1. 模組地圖

```
game/busted99/
  session.py       — 純 dataclass，只存狀態，不含邏輯
  scoring.py       — score_for_space(space)，不依賴任何其他模組
  engine.py        — 主狀態機，parse_number，Busted99Engine
  llm_engine.py    — Busted99LLMEngine(Busted99Engine)，覆寫 submit_guess
  voice_parse.py   — extract_guess_via_llm：STT 文字 → 數字（3-layer LLM fallback）
  marvin99.py      — Marvin99：Marvin 作為猜題人時的垃圾話 LLM

cogs/busted99_cog.py
  — Discord UI 層（slash commands、embeds、views、TTS 觸發、WS 廣播）
  — 是 Discord 和引擎之間唯一的膠水，引擎不 import discord
```

**共用 game/ 基礎設施（全遊戲共享）：**

```
game/player_score_db.py   — 跨遊戲積分持久化
game/game_memory_db.py    — Marvin 記憶體 context（write_event / get_context_block）
```

---

## 2. 狀態機

```
IDLE → JOINING → SETTER_PICKING → GUESSING ⟲ → GAME_OVER
```

- `IDLE`：初始態，`engine` 不存在
- `JOINING`：玩家按 Join 按鈕；35 秒後自動 `start_game()`
- `SETTER_PICKING`：抽出 setter，等待出題；60 秒無操作 → 隨機 fallback
- `GUESSING`：每輪一人猜題，答對 → GAME_OVER，答錯 → `_advance_guesser()` 循環
- `GAME_OVER`：清理 engine、輸出排行榜

每次 state 轉換都呼叫 `on_state_change(session)` callback，cog 在這裡更新 Discord embed、廣播 WS、觸發 TTS。

**重要**：engine 永遠不直接呼叫 Discord API，它只呼叫注入的 `on_state_change` callback。

---

## 3. 分數設計（反直覺是核心）

```python
score_for_space(space) = max(10, min(100, 100 - (space // 10) * 10))
```

| space  | 分數 |
|--------|------|
| 90-99  | 10   |
| 10-19  | 90   |
| 1-9    | 100  |

**反直覺設計**：

| 結果         | 猜題人  | 其他玩家              |
|--------------|---------|----------------------|
| `bust`       | 0 分    | +score_for_space(space) |
| `last_bust`  | 0 分    | setter +100，其他 +score |
| `last_wrong` | +100 分 | 0                    |
| `wrong_low/high` | 0   | 0（範圍縮小，換人）  |
| `timeout`    | -score  | 0                    |

猜中 = 爆炸 = 零分。最後 2 選 1 猜錯反而得 100 分。這個反直覺設計是遊戲趣味的核心，**LLM narration 的情感邏輯必須完全對齊**，否則台詞會說錯慶祝方向。

---

## 4. Engine / LLMEngine 繼承分層

```python
class Busted99Engine:         # code-only 裁判
    submit_guess(...)         # 純數學判定
    timeout_guesser(...)      # 扣分 + 推進
    add_player(...)           # 共用
    start_game(...)           # 共用
    set_answer(...)           # 共用

class Busted99LLMEngine(Busted99Engine):
    submit_guess(...)         # 覆寫：呼叫 LLM + code 驗證
    # 其他方法完全繼承
```

**關鍵設計原則**：

1. **LLM 只做 outcome + narration，分數永遠由 code 計算**，不信 LLM 算數。
2. **LLM outcome 有 code 驗證（`_ok` 交叉檢查）**：若 LLM 說 `wrong_low` 但 `number >= answer`，丟棄 LLM 結果，fallback `_adjudicate()`。
3. **LLM call 在 lock 外**：submit_guess 先取 low/high 快照，出 lock，call LLM（5s），再進 lock 比對狀態（TOCTOU 防護）。
4. **Cog 可零改動切換 engine**：`BUSTED99_LLM=true` 環境變數決定使用哪個 class，cog 只看 `engine.submit_guess()` 的 return dict。

**LLM 3-layer fallback（兩個地方都有）**：

```
Cerebras Qwen-3-235B → Groq Llama-3.3-70B → Gemini 2.5 Flash
```

- `llm_engine.py`：submit_guess 的 narration + outcome（長 prompt，few-shot）
- `voice_parse.py`：STT 文字 → 數字（短 prompt，只抽 number）

---

## 5.鎖與並發

**engine 有一把 `asyncio.Lock`（`self._lock`）**，保護所有 state 修改：

```python
async with self._lock:
    # 讀 state → 決策 → 改 state
    ...
await self._on_state_change(session)   # 在 lock 外廣播
```

**LLMEngine 的 TOCTOU 防護**：

```python
# 第一次進 lock：讀快照，出 lock
async with self._lock:
    low, high = session.low_bound, session.high_bound
    guesser_id_check = session.current_guesser_id

# LLM call（可能 5s）

# 第二次進 lock：重新驗 state，防止 LLM 期間 timeout_guesser 已跑
async with self._lock:
    if session.state != Busted99State.GUESSING:
        return self._quick_reply("invalid_state")
    if session.current_guesser_id != guesser_id:
        return self._quick_reply("invalid_guesser")
```

**cog 的 task 管理**：

```python
self._tasks: set[asyncio.Task] = set()

def _spawn(self, coro):
    t = asyncio.get_running_loop().create_task(coro)
    self._tasks.add(t)
    t.add_done_callback(self._tasks.discard)
    return t
```

`_cancel_tasks()` 在 GAME_OVER 和 `end_session` 時清掉所有 background task。

---

## 6. DB 設計與 threading

**問題**：SQLite 不是 async，engine 在 asyncio event loop 裡。

**解法**：全部用 `loop.run_in_executor(None, self._save_xxx)` 丟到 thread pool：

```python
loop = asyncio.get_running_loop()
loop.run_in_executor(None, self._save_guess, ...)   # fire-and-forget
```

**不 await**：DB 寫入是 fire-and-forget，不阻塞遊戲流程。代價是：DB 寫入順序不保證，crash 時最後幾筆可能丟失。對遊戲場景可接受。

**scores snapshot 問題**：

```python
# 必須在 lock 內快照，lock 外不能碰 session.players
all_scores_snap = json.dumps({p.display_name: p.score for p in self.session.players})
loop.run_in_executor(None, self._save_guess, ..., all_scores_snap)
```

如果在 `run_in_executor` 的 lambda 裡讀 `self.session.players`，那時 lock 已釋放，session 可能被新一局覆蓋。

**SQLite migration（ADD COLUMN）**：

```python
try:
    conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {defn}")
except sqlite3.OperationalError:   # 欄位已存在，靜默跳過
    pass
```

用 `sqlite3.OperationalError` 不是 `Exception`，避免掩蓋真正的 DB 錯誤。

---

## 7. 語音整合

### STT 過濾（非猜題者早期丟棄）

**兩層過濾**：

```
Layer 1 — discord_voice_engine._flush_audio（by user_id int）：
  _b99.should_suppress_for_game_by_id(user_id) → bool
  → 在 _full_stt_inflight += 1 之前 return，不佔名額

Layer 2 — cog.receive_voice_answer_by_speaker（by display_name str）：
  guesser.display_name != speaker → return False
```

Layer 1 更早，節省 STT inflight 容量。Layer 2 是保底，因為 `display_name` 更精確（同 user_id 但顯示名不同的邊角 case）。

**`should_suppress_for_game_by_id` 設計**：

- 輸入是 `int`（Discord user_id），session 存的是 `str` → 用 `str(user_id) != str(guesser_id)` 比較
- `_advance_guesser()` 是同步的，在 `submit_guess` 的 lock 內執行，所以 `current_guesser_id` 在 lock 釋放前就已更新，flush 時讀到的 always 是最新值

### TTS 優先權（`_tts_protected`）

遊戲 narration 有優先權：

```python
async def _fire_tts(self, vc, text: str) -> None:
    vc._tts_protected = True        # bypass silence gate
    await vc.play_tts(text, force_macos=True)  # bypass game_mode drop
    vc._tts_protected = False
```

`force_macos=True`：走本機 macOS say（100-300ms），不走雲端 TTS，避免玩家說話時被 Echo Guard 吃掉。

### range TTS + narration 不競爭

wrong_low/wrong_high 要先播「範圍縮小 X 到 Y」，再播 LLM narration，兩段不能競爭播放佇列：

```python
async def _range_then_narration(vc=vc, rt=range_text, nt=narration):
    await self._fire_tts(vc, rt)    # sequential, not concurrent
    if nt:
        await self._fire_tts(vc, nt)

self._spawn(_range_then_narration())
```

**不要**用兩個獨立的 `self._spawn(self._fire_tts(...))` 並行觸發，它們會競爭佇列。

### game_mode_cap

遊戲進行時壓低 VAD 靜默門檻（cap=0.8）：

```python
engine.conv_buffer.game_mode_cap = 0.8   # 開始
engine.conv_buffer.game_mode_cap = None  # 結束
```

目的是讓玩家說短數字（「四十二」=0.4-0.6s）不被「高溫對話截斷」的靜默等待吃掉。

---

## 8. Web UI（WebSocket state）

**設計原則**：cog 暴露一個穩定的 view model（`_build_ws_state()`），前端只訂閱這一份 JSON，不直接讀 session 欄位。

```python
def _build_ws_state(self, session) -> dict:
    return {
        "type": "game_state",
        "phase": ...,
        "round": ...,
        "guesser": ...,
        "range_low": ..., "range_high": ...,
        "remaining_sec": ...,
        "scores": [...],
        "players": [...],
        "skip_votes": ..., "skip_votes_needed": ...,
        "answer": ...,          # 只有 game_over 才非 None
        "guess_log": [...],     # 最近 50 筆
    }
```

**guess_log** 最多 50 筆傳給前端（payload 限制），engine 內保留最多 200 筆（記憶體限制）。

**HTML XSS 防護**：

```js
function safeText(str) {
    const d = document.createElement('div');
    d.textContent = String(str);
    return d.innerHTML;
}
const _GL_SAFE_CLASSES = new Set(['wrong_low','wrong_high','bust','last_bust','last_wrong','timeout']);
```

所有 user-controlled 字串（`guesser`、`p.name`）經 `safeText()` escape。CSS class 名稱用 whitelist 驗證。數值欄位用 `Number()` 強制型別（防止字串注入）。

---

## 9. 踩到的雷

### 雷 1：`Busted99Session.__new__()` 繞過 dataclass defaults

測試常用 `__new__` 構造 session 以避免資料庫初始化，但 `__new__` 不執行 `__init__`，所以 `field(default_factory=list)` 的欄位（`guess_log`、`guesser_order`、`guessing_queue`）都不會被初始化。

**症狀**：engine 嘗試 `session.guess_log.append(...)` → `AttributeError`。

**修法**：測試 helper 在 `__new__` 後手動設：

```python
s.guess_log = []
s.guesser_order = []
s.guessing_queue = []
```

**原則**：每次 session dataclass 加新 list/dict 欄位，同步更新所有用 `__new__` 的 test helper。

---

### 雷 2：`guesser_id` 在 embed 建立時已推進

`_advance_guesser()` 在 `submit_guess` 內（lock 裡）同步執行，回傳 result dict 時 `session.current_guesser_id` 已是「下一個」猜題人。

**症狀**：embed 顯示的猜題人名字是下一個人，不是剛才猜的人。

**修法**：在 `submit_guess` lock 內、`_advance_guesser()` 之前抓 `guesser_name`：

```python
guesser_name = next(p.display_name for p in session.players if p.user_id == guesser_id)
# ... advance ...
result = {"guesser_name": guesser_name, "guesser_id": guesser_id, ...}
```

embed builder 優先用 `result["guesser_name"]`，不讀 `session.current_guesser_id`。

---

### 雷 3：LLM `last_wrong` 幻覺（space > 2 時錯誤輸出 last_wrong）

LLM 有時在 space=10 時就回 `last_wrong`，讓猜題人得到不應得的 100 分。

**修法**：code 交叉驗證，不信任 LLM 的終局判定：

```python
_ok = (
    (llm_outcome == "wrong_low" and number < answer and space > 2)
    or (llm_outcome == "wrong_high" and number > answer and space > 2)
    or (llm_outcome in ("bust", "last_bust") and number == answer)
    or (llm_outcome == "last_wrong" and space <= 2 and number != answer)
    or llm_outcome == "boundary"
)
if not _ok:
    llm_outcome, _, _ = self._adjudicate(low, high, answer, number)
```

**原則**：LLM 做主持，code 做裁判。分數、outcome 以 code 規則為準。

---

### 雷 4：async closure 的 late-binding 陷阱

```python
# 錯誤！rt 和 nt 在閉包執行時才查值，可能已被下個回合覆蓋
async def _range_then_narration():
    await self._fire_tts(vc, range_text)
    await self._fire_tts(vc, narration)

# 正確：用預設參數捕捉當下的值
async def _range_then_narration(vc=vc, rt=range_text, nt=narration):
    await self._fire_tts(vc, rt)
    await self._fire_tts(vc, nt)
```

這在 `_spawn()` 用 fire-and-forget 時最容易踩，因為 task 被延遲執行時外層變數可能已改變。

---

### 雷 5：TTS 播放競爭

用兩個獨立 `self._spawn()` 觸發的 TTS 會「搶」同一個播放佇列，實際播出順序不保證：

```python
# 雷：順序不保證
self._spawn(self._fire_tts(vc, range_text))
self._spawn(self._fire_tts(vc, narration))

# 正確：sequential task
async def _seq(vc=vc, rt=range_text, nt=narration):
    await self._fire_tts(vc, rt)
    if nt:
        await self._fire_tts(vc, nt)
self._spawn(_seq())
```

---

### 雷 6：`on_state_change` 廣播觸發順序

`_guessing_deadline` 在 `on_state_change(GUESSING)` 裡設定，但 WS broadcast 在 deadline 設定之前就已跑（`_emit_ws_state` 在 `on_state_change` 開頭）。

**症狀**：web UI 第一次收到 GUESSING 狀態時 `remaining_sec` 是 0。

**修法**：進入 GUESSING 後再手動 broadcast 一次：

```python
elif state == Busted99State.GUESSING:
    self._guessing_deadline = time.time() + 600.0
    await self._emit_ws_state(session)   # 補播一次，這時 deadline 已設
```

---

### 雷 7：`timeout_guesser` 沒有 `guesser_name`（embed 顯示 unknown）

timeout 的 embed 需要「剛才超時的人」的名字，但 `timeout_guesser()` 在 `_advance_guesser()` 後才回傳，`current_guesser_id` 已是下一個人。

**修法**：在 `_advance_guesser()` 之前抓 `timed_out_name`，放進 return dict：

```python
timed_out_name = next(p.display_name for p in players if p.user_id == guesser_id)
self._advance_guesser()
return {"timed_out_name": timed_out_name, ...}
```

embed builder 用 `result.get("timed_out_name")`。

---

## 10. 下一個遊戲可以複製什麼

### 可以直接複製（改名即可）

| 元件 | 說明 |
|------|------|
| `session.py` dataclass 結構 | state enum + players list + 分數欄位 |
| `_spawn()` task 管理 | 避免 task 洩漏，GAME_OVER 時 cancel_tasks |
| `on_state_change` callback pattern | engine 不 import discord |
| `loop.run_in_executor()` DB writes | fire-and-forget，不阻塞 event loop |
| `_upsert_game_message()` | edit-in-place，避免 embed 刷新後跑到頂 |
| `_tts_protected` + `force_macos=True` | 遊戲 narration bypass silence gate |
| `_fire_tts` sequential pattern | range + narration 不競爭播放佇列 |
| `should_suppress_for_game_by_id(int)` | Engine 層早期 STT 過濾 |
| `_build_ws_state()` view model | 穩定的前端 payload，不直接暴露 session |
| HTML `safeText()` + class whitelist | innerHTML XSS 防護 |
| `except sqlite3.OperationalError` migration | ADD COLUMN 靜默跳過 |

### 需要客製化

| 元件 | 說明 |
|------|------|
| `scoring.py` | 不同遊戲有不同分數邏輯 |
| LLM system prompt | 情感邏輯、遊戲規則、few-shot 例子 |
| `_ok` 驗證規則 | 每個遊戲的 outcome 集合不同 |
| `guess_log` schema | 記錄哪些欄位視遊戲而定 |
| `Marvin` AI 人格邏輯 | 垃圾話 context 依遊戲局勢不同 |

### 共用基礎設施（已存在 `game/`）

- `player_score_db.py`：`add_scores(con, deltas)` — 跨遊戲積分持久化
- `game_memory_db.py`：`write_event(con, text)` / `get_context_block(db_path, n)` — Marvin 記憶

下一個遊戲的 engine 直接呼叫這兩個，不需要自己建表。

---

## 附錄：LLM Engine 設計 checklist

給下一個遊戲的 LLM engine 作者：

- [ ] LLM call 在 lock **外**
- [ ] 進 lock 前和進 lock 後各驗一次 state（TOCTOU）
- [ ] outcome 用 code 交叉驗證，拒絕數學矛盾的 LLM 回答
- [ ] 分數計算完全在 code，不從 LLM response 讀數字
- [ ] LLM JSON 解析失敗時有 `_adjudicate()` code fallback
- [ ] 3-layer LLM fallback：Cerebras → Groq → Gemini
- [ ] LLM prompt 的情感邏輯必須與分數設計對齊
- [ ] `guesser_name` 在 `_advance_guesser()` **之前**抓
- [ ] `narration` 空字串不 fire TTS（`if nt:` 檢查）
