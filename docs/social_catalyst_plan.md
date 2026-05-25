---
title: Social Catalyst — 三核心 Agent 計劃
status: in_progress
owner: jack
started: 2026-05-25
---

# 目標

把音樂房從「bot 表演」變成「人類互相聊天 bot 當觸媒」。
**不擴張房間，先把核心 agent 做到極致。**

## 北極星指標

一個月後，問音樂房用戶「最近你跟 X 講了什麼」，
若回答內容是 **人 ↔ 人** 的對話（不是 bot 點了什麼歌）→ 成功。

---

# 三個核心 Agent

| Agent | 職責 | Bus |
|---|---|---|
| **BridgeAgent** | 把人類的話題丟給彼此（callback / seed / drama） | SpeakBus 主 + IntentBus 副 |
| **MoodAgent** | 讀情緒（個體 + 群體 + prosody），決定行動分級 | SpeakBus 主 + IntentBus 副 |
| **DuckingAgent** | 偵測「人類正在熱聊」→ 全面壓制 bot 主動發話 + 提高 wake 門檻 | SpeakBus 抑制器 |

## 為什麼是 3 個不是 5 個

之前列的 Drama / Tradition 是 BridgeAgent 的 mode，不獨立。
共享 store 的 agent 不該拆，會 bid 衝突 + 重複計算 embedding。

---

# 共享基建（先寫，三 agent 才能各自做到極致）

## 1. `SpeakerTopicGraph` (sqlite)

每個 utterance 寫入：
```
speaker         TEXT   -- Discord username
text            TEXT   -- cleaned text
topic_embedding BLOB   -- 384-dim float32 (sentence-transformers MiniLM)
emotion_text    TEXT   -- mood label from MoodAgent (nullable)
emotion_prosody TEXT   -- prosody label (nullable)
guild_id        TEXT
channel_id      TEXT
created_at      INTEGER -- unix ts
```

索引：`(channel_id, created_at DESC)` for room query；`(speaker, created_at DESC)` for per-user query。

API:
- `record(speaker, text, channel_id, ...)` — 同步寫入（每 utterance 一次）
- `find_similar(text, channel_id, exclude_speaker, window_days=30) -> list[(speaker, text, ts, similarity)]`
- `recent(channel_id, n=20) -> list[Row]`
- `speaker_topics(speaker, channel_id, n=50) -> list[Row]`

檔案位置：`speaker_topic_graph.py`（新檔，根目錄）
DB 位置：沿用 `marvin.db` 加 table，不開新 DB。

## 2. `SpeakBus`

**主動發話的 bid 架構**，類似 IntentBus 但是 proactive，由 timer / event 觸發。

```python
class SpeakBus:
    def register(self, agent: SpeakAgent) -> None
    async def tick(self, ctx: SpeakContext) -> SpeakBid | None
    def set_global_multiplier(self, m: float) -> None  # DuckingAgent 抑制全局
```

`SpeakContext` 包含：當前 room state、recent transcripts、room mood、靜默秒數。
`SpeakBid` = `(agent_name, confidence, handler, reason, ttl_s)`。

觸發點（誰呼叫 `tick()`）：
- voice_controller idle loop（每 5s）
- 一句話講完後 2s（給 BridgeAgent callback 機會）
- mood transition（MoodAgent 觸發其他 agent 重新 bid）

檔案位置：`speak_bus.py`（新檔，根目錄）

## 3. `RoomMoodState`（記憶體 + 5min 持久化）

```python
@dataclass
class RoomMoodState:
    channel_id: str
    individual_mood: dict[str, MoodLabel]   # speaker → 4 檔 mood
    group_mood: MoodLabel                    # 房間整體
    group_temperature: float                 # 0.0-1.0，沿用 discord_temperature_monitor
    hot_chat: bool                           # 高熱聊 flag（DuckingAgent 用）
    hot_chat_pair: tuple[str, str] | None    # 正在熱聊的兩人
    updated_at: float
```

寫者：MoodAgent + DuckingAgent
讀者：BridgeAgent + 三 agent 互相讀
持久化：每 5 分鐘 dump 到 `data/room_mood_state.json`（重啟用）

檔案位置：`room_mood_state.py`（新檔，根目錄）

---

# Agent 規格

## DuckingAgent（**先做**，週 2）

### 偵測規則

**高熱聊狀態 = 兩個 speaker 在 15 秒內交替發話 ≥3 次**

```python
# 偽碼
turns = recent_turns(channel_id, window_s=15)
if len(turns) >= 3:
    speakers = [t.speaker for t in turns]
    if len(set(speakers[-3:])) == 2 and speakers[-1] != speakers[-2]:
        hot_chat = True
        hot_chat_pair = tuple(sorted(set(speakers[-3:])))
```

### 行動

1. `SpeakBus.set_global_multiplier(0.2)` — 所有 SpeakBid 信心 × 0.2
2. IntentBus wake word 信心閾值 + 0.1（透過 ctx 提示，不改 IntentBus 本體）
3. 若 bot 正在 TTS 播放且偵測到人類插話 → fade-out（不硬切）
4. 解除條件：靜默 5s OR 被點名 OR 5 分鐘 timeout

### 量化目標

- 「bot 搶話」用戶抱怨 → **歸零**
- 高熱聊期間 bot 主動發話 → **每分鐘 ≤ 0.2 次**

### 不變式（其他 session 不能違反）

- DuckingAgent **不該** 自己發話。它只壓制其他 agent
- TTS fade-out 必須走現有 `playback_lock` 鏈，**不繞過**
- wake 閾值調整僅透過 ctx hint，**不改 IntentBus.MIN_CONFIDENCE 常數**

---

## MoodAgent（週 3）

### 三軸偵測

| 軸 | 來源 | 既有檔 |
|---|---|---|
| 文字情緒 | LLM 4 檔分類（放鬆/興奮/低落/分歧） | `mood_sensor.py` ✅ |
| Prosody 情緒 | STT meta（avg_confidence / speaking_rate / pause） | `protocols.py` meta dict ✅ |
| 群體溫度 | `discord_temperature_monitor.temperature` | ✅ |
| 時段 | unix ts → 時段 bucket（晨/午/晚/深夜） | 新增 |

### 行動分級

| 等級 | 條件 | 行動 |
|---|---|---|
| 輕度 | 1 人輕微 down | 給 MusicAgent hint，下首歌調整 |
| 中度 | 多人悶 / 群溫 ↓ | 給 BridgeAgent hint，丟輕鬆 seed |
| 重度 | 明確負面 + 群體靜默 ≥ 60s | 通知 DuckingAgent 進入「靜默尊重」模式，可選 DM 私訊 |

### 不變式

- **不主動說「你還好嗎」** — 中文圈尷尬
- 不寫 bot 自己的 mood 進 graph，只寫人類的
- MoodAgent 自己**不發話**，發 hint 給其他 agent；發話權歸 BridgeAgent / MusicAgent

---

## BridgeAgent（週 4）

### 三種橋接動作

#### A. Callback bridge（最有黏性）

A 講完 → 用 topic embedding 在 `SpeakerTopicGraph` 找：
- 過去 30 天
- 同 channel
- **不是** A 自己
- 在場（recent active speaker 列表）
- topic 相似度 ≥ 0.65

找到 → 2-5 秒內 bridge：「Alice 上週也在 rant 主管耶，你們倆要不要對一下」

#### B. Cold-start seed

冷場 ≥ 90s（且非 hot_chat 期間）→ 從在場人的 recent topics 抽 1 個共同主題 → 開放問題。

#### C. Opinion drama

偵測在場人有對立意見（embedding 距離大 + 同 topic 的 sentiment 反向）→ 製造輕度辯論。

### 不變式

- bridge 句型**不是質問**，是 setup。把球丟到兩人中間，不是 bot 問 Alice
- **同 callback 30 天不重用**（記在 graph 上 last_bridged_at）
- bridge 後 15s 內若被橋的人沒開口 → 不追擊
- 沒 hot_chat 衝突檢查就不能發話：每次發話前 check `RoomMoodState.hot_chat`

### 量化目標

- Bridge 後 15s 內被橋的人開口 → **≥ 40%**

---

# 執行順序

```
Week 1 — 基建（無 user-visible 行為改變）
  □ SpeakerTopicGraph schema + 寫入路徑（hook 進 voice_controller transcript flow）
  □ embedding 服務（sentence-transformers MiniLM-L6，本地，~80MB）
  □ SpeakBus 骨架（register / tick / set_global_multiplier）
  □ RoomMoodState 結構 + 5min dump

Week 2 — DuckingAgent
  □ turn-taking 偵測（從 SpeakerTopicGraph 拉 recent）
  □ SpeakBus 全局 multiplier 接上
  □ TTS fade-out（接 playback_lock）
  □ test: tests/test_ducking_agent.py（5 類：detection / multiplier / fadeout / cooldown / wake_hint）
  □ 線上觀察 1 週「搶話抱怨」歸零

Week 3 — MoodAgent
  □ Prosody 從 STT meta 抽出（沿用 commit 51771d8 路徑）
  □ 三軸合成（文字 + prosody + 群溫）
  □ 行動分級 hint bus（簡單 pub-sub）
  □ 接 MusicAgent / BridgeAgent / DuckingAgent
  □ test: tests/test_mood_agent.py

Week 4 — BridgeAgent
  □ embedding 相似度查詢 + 在場過濾
  □ Callback bridge 句型 prompt（小心，要繁中 + 不質問）
  □ Cold-start seed
  □ Opinion drama（可選，最後做）
  □ cooldown 機制
  □ test: tests/test_bridge_agent.py
  □ 線上觀察 bridge 後開口率
```

---

# 跨 Session 防護（不變式）

下面這些是 **任何 session 都不該違反** 的——讀到這份計劃的下一個 session 要 honor：

## 架構不變式

1. **DuckingAgent 是壓制器，不是發話者** — 它的 SpeakBid confidence 永遠 0，只動 multiplier
2. **MoodAgent 不發話** — 只發 hint 給其他 agent，避免「機器讀情緒 → 機器回情緒」的尬聊
3. **BridgeAgent 不質問** — 句型必須是 setup（把球丟向兩人之間），不是「你也是嗎」式提問
4. **三 agent 共用 `SpeakerTopicGraph` 為唯一社交記憶來源** — 不要新建第二份
5. **SpeakBus 跟 IntentBus 分離** — 一個 proactive 一個 reactive，不合併
6. **`playback_lock` 鏈不可繞過** — TTS fade-out 必須走原鏈
7. **`marvin.db` 加 table 不開新 DB** — 沿用既有 sqlite 連線
8. **不改 IntentBus.MIN_CONFIDENCE 常數** — wake 門檻調整透過 ctx hint

## 既有元件不該動

- `mood_sensor.py`：MoodAgent 是「上層消費者」，內部 LLM 分類器保留現狀
- `discord_temperature_monitor.py`：MoodAgent 只讀 `.temperature`，不改
- `intent_bus.py`：不動，SpeakBus 平行存在
- `intent_agents/base.py`：DeclarativeIntentAgent 模式保留給 reactive intent，新三 agent **不繼承它**（它們是 SpeakAgent）
- 既有 game agents (`busted/busted99/turtle_soup`)：完全不動

## 觀察口（debug 友善）

每個 agent 必須寫到 `records/social_catalyst.jsonl`，欄位：
```json
{"ts": 1716600000, "agent": "DuckingAgent", "event": "hot_chat_on",
 "channel_id": "...", "pair": ["A","B"], "reason": "3 turns in 12s"}
```

---

# 北極星量化（一個月後評估）

| 指標 | 目標 |
|---|---|
| 「bot 搶話」抱怨 | 0 件/週 |
| Bridge 後被橋的人 15s 內開口 | ≥ 40% |
| Mood 行動誤判（用戶抱怨「我哪有不開心」） | < 1/週 |
| 用戶能講出「跟 X 聊了 Y」（非 bot 內容） | 5+/週訪談 |

---

# 風險與 fallback

- **embedding 服務失敗** → BridgeAgent 降級為「純 recency-based」（拿在場人最近 5 句的關鍵字當話題種子）
- **SpeakBus 全壞** → 退回現況（bot 只做 reactive intent），不影響音樂房
- **DuckingAgent 過敏** → 全局 multiplier 不要直接 0.2，先 0.5 灰度
- **Mood LLM 連續失敗** → 沿用 mood_sensor.py 既有的 stale cache + DEFAULT_MOOD fallback
