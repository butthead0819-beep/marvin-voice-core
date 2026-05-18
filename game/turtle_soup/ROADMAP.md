# 海龜湯 ROADMAP — v1 之後的需求規劃

把未來要做的事先寫下來，這樣 v0 實作時不會誤把 v1 功能塞進來。
每個版本都對應一個**單一驗證假設**——驗證通過才往下做。

| 版本 | 主要驗證 | 預估工時 | 觸發條件 |
|---|---|---|---|
| [v0](#v0-mvp-當前) | STT 自由問句 + LLM judge loop 能跑 | 7-8h | 已啟動 |
| [v1](#v1-單題-ux-完整化) | 玩家一題玩到爽，會想再玩 | 12-16h | v0 驗收 A1-A10 全綠 + 真實玩家測試 ≥ 5 場 |
| [v2](#v2-題庫與重玩性) | 玩家連玩 10 場不膩 | 20-30h | v1 驗收：5 場後玩家會問「有沒有別題」 |
| [v3](#v3-多人競技與觀眾參與) | 能成為 streamer 固定節目 | 30-50h | v2 驗收：streamer 願意每週開一場 |
| [v4](#v4-創作者經濟) | 使用者生成內容（UGC）成立 | 40-60h | v3 驗收：streamer / 觀眾要求出題 |
| [v5](#v5-streamer-個人化) | 變成 streamer 的個人化資產 | 30-40h | v4 驗收：UGC 題目品質夠 |

不是線性。發現假設不成立就停下檢討，不是硬推下一版。

---

## v0 (MVP) — 當前

見 [REQUIREMENTS.md](./REQUIREMENTS.md) 與 [ARCHITECTURE.md](./ARCHITECTURE.md)。

**驗證假設**：STT 自由問句 + LLM judge loop 在 Discord 語音場景能跑。

**預期產出**：一場完整可玩遊戲、1 題 hardcoded、3 verdict、無計分、無 hint、無 dispute。

---

## v1: 單題 UX 完整化

**驗證假設**：在「同一題」的體驗範圍內，玩家會覺得 Marvin 是有趣的主持人，會想繼續挑戰。

**前置條件**：v0 驗收 A1-A10 全綠，且至少跑過 5 場真實多人遊戲。

### 新增功能

#### 1. 5-Verdict 細分
從 3 verdict 擴成 5：
- `yes` — 完全符合湯底
- `no` — 與湯底矛盾
- `close` — 方向對但不夠精確（**新**）
- `important` — 命中關鍵事實之一（**新**）
- `irrelevant` — 不是是非題或與真相無關

新 SFX 對應：
- `close` → `building.wav`（新增，鼓點漸快感）
- `important` → `bell_strong.wav`（新增，提示這是進度條訊號）

prompt 改寫：需要更精準的 verdict 判定規則。REPL 重新校準。

#### 2. ~~Hint 系統~~（已於 v0.3+v0.4 提前實作）
~~v1 規劃：玩家 60 秒無動作 → Marvin 主動丟一個方向提示；玩家可主動 `/turtle_hint`~~

**實際進度**：
- **v0.3**：玩家主動「請問給我提示」+ 60s idle timer + engine.request_hint pop list
- **v0.4**：題目設計時用 LLM 產 1D/2D/3D 三維 hint 候選
  - `game/turtle_soup/hint_generator.py` + `scripts/generate_puzzle_hints.py`
  - 維度定義：1D 指類別、2D 連兩元素、3D 點機制
  - 離線生成（作者人工挑選後寫入 puzzles.py）
- **v0.5**：hint 編織網模型（HintNode + Hint 圖結構）
  - 節點 + 揭露關係取代線性 list
  - LLM 一次 call 同時 top-down 抽節點 + bottom-up 組提示
  - _validate 鎖 5 個不變式
- **v0.6（前 v1）**：個人化 hint 排序
  - HintNode.keywords：玩家問題 keyword 命中 → 視為已探索此節點
  - Engine 用資訊增益演算法：選 new_nodes 最少（最循序漸進）的 hint
  - 支援分支（非線性）puzzle：multi-branch / 非相鄰 reveals 自動處理
  - Session.given_hint_indices 防重複給

v1 剩餘升級：runtime lazy 生成（給 v4 UGC 用），讓玩家投稿題目時自動產 hints + keywords

#### 3. Dispute 機制
玩家覺得 Marvin 判錯時：
- 喊「我不同意」/「Marvin 你錯了」/`/turtle_dispute`
- 觸發第二次 LLM judge（不同 prompt，要求 LLM「再仔細想一次」）
- 第二次 verdict 若與第一次不同 → 公開承認改判（增加遊戲幽默感）
- 若一致 → Marvin 維持原判，可加吐槽

**風險**：dispute 太常被濫用會拖延。設冷卻時間（每場 3 次）。

#### 4. Verified Q&A 鎖
題目格式擴充：
```python
PUZZLE = {
    "surface": "...",
    "truth": "...",
    "key_facts": [...],
    "verified_qa": [
        {"q_pattern": ["他是侏儒嗎", "他是矮個子嗎"], "verdict": "yes"},
        {"q_pattern": ["電梯壞了嗎"], "verdict": "no"},
        ...
    ],
}
```

LLM judge 前先 keyword/embedding 比對 `verified_qa`，命中就直接用 verified verdict 跳過 LLM。

**好處**：作者可保證關鍵問題判定正確、節省 LLM 成本（命中率約 30-50%）。

#### 5. 結束畫面與統計
- 顯示總提問數、verdict 分布、最有趣的 narration（玩家投票）、最終答案完整版
- embed 加上玩家可截圖分享的 layout

### 不做（留給 v2）

- 多題切換（v1 仍然只有 1-3 題 hardcoded）
- 計分排行
- 觀眾參與

### v1 驗收

- [ ] 5-verdict prompt 跑過 30 個校準問題，正確率 ≥ 75%
- [ ] Hint 系統觸發率：玩家自發喊 hint / 全場 ≥ 30%（代表有效）
- [ ] Dispute 觸發後改判率：< 20%（代表第一次 judge 已可靠）
- [ ] verified_qa 命中率：≥ 40%（驗證機制有效）
- [ ] 連續玩 3 場同一題的玩家比例 ≥ 30%（代表玩家覺得有趣）

---

## v2: 題庫與重玩性

**驗證假設**：玩家連玩 10 場不會膩，會持續回來玩。

**前置條件**：v1 上線後真實玩家測試 ≥ 5 場，且開始要求「換題」。

### 新增功能

#### 1. Puzzle Bank
位置：`assets/turtle_soup/puzzles.json`

格式：
```json
[
  {
    "id": "elevator_18f",
    "surface": "...",
    "truth": "...",
    "key_facts": [...],
    "verified_qa": [...],
    "difficulty": "easy",      // easy | medium | hard
    "tags": ["lateral", "physical"],
    "author": "marvin_team",
    "created_at": "2026-05-17",
    "play_count": 0,           // 全域累積（之後做）
    "avg_questions_to_solve": null
  },
  ...
]
```

種子題庫：20 題人工撰寫 + REPL 校準。

#### 2. 題目選擇 UI
- `/turtle_soup_start` 預設隨機抽
- `/turtle_soup_start difficulty:hard` 指定難度
- `/turtle_soup_browse` 看題庫清單（已玩過的灰掉）

#### 3. 玩家題庫進度
DB schema (`marvin.db`)：
```sql
CREATE TABLE turtle_soup_plays (
  user_id TEXT,
  puzzle_id TEXT,
  played_at REAL,
  outcome TEXT,         -- win / surrender / exhausted
  questions_count INT,
  hints_used INT,
  PRIMARY KEY (user_id, puzzle_id, played_at)
);
```

玩家可查詢「我玩過幾題、勝率多少」。

#### 4. 題目難度自動校準
跑一陣子後：
- 平均提問數 < 8 題 → 自動標 `easy`
- 8-20 題 → `medium`
- > 20 題 → `hard`
- 投降率 > 50% → 標 `tough`

#### 5. 共用基建抽離
這版開始把 `llm_judge` 內的 3-layer fallback 抽到 `game/common/llm_judge_base.py`，給狼人殺與其他遊戲共用。

### 不做（留給 v3）

- 計分 / 排行榜
- Twitch chat 參與
- LLM 出題

### v2 驗收

- [ ] 種子題庫 20 題全部跑過至少 3 場玩家測試
- [ ] 平均單場時長：15-25 分鐘（甜蜜區間）
- [ ] 同一玩家平均單週玩 ≥ 3 場
- [ ] 玩家通關 / 投降 比例：60/40 ~ 80/20（過難或過簡都不對）

---

## v3: 多人競技與觀眾參與

**驗證假設**：海龜湯能成為 streamer 的固定節目模式，吸引觀眾。

**前置條件**：v2 上線後有 streamer 主動要求「能不能加分數」。

### 新增功能

#### 1. 計分系統
- 基礎分 100，每問一題扣 1 分（鼓勵高效推理）
- Hint 扣 5 分
- Dispute 扣 3 分
- 猜中 +50 bonus
- 用最少問題猜中的玩家拿全分，其他人按比例
- 結算後寫入 `game/player_score_db.py`（沿 Busted99 模式）

#### 2. Tournament Mode
- `/turtle_soup_tournament rounds:5`
- 連續 5 題，累積分數，最高分勝
- 中場休息時 Marvin 念分數排行 + 講垃圾話

#### 3. Twitch Chat 參與
- 觀眾在 Twitch chat 發訊息 → bot 同步到 Discord
- 觀眾可投票表決：當前提問是否「good question」（用於後續題目品質統計）
- 觀眾可送 super hint（用 channel point）
- 整合 [twitch_collector](../../scripts/twitch_collector.py) 既有基建

#### 4. Streamer 工具
- `/turtle_soup_export` — 把這一場的 transcript + 結果存 JSON
- Web UI dashboard：歷史場次、最佳 narration 集錦、玩家數據
- Stream overlay：browser source URL 顯示當前題目進度 + 玩家排名

### v3 驗收

- [ ] 至少 1 個外部 streamer 把海龜湯排進固定節目表
- [ ] 單場觀眾平均互動 ≥ 5 次（投票 / hint / 評論）
- [ ] Tournament 模式被完成率 > 70%（不會玩到一半放棄）
- [ ] Twitch chat 同步延遲 < 3 秒

---

## v4: 創作者經濟（UGC）

**驗證假設**：玩家 / 觀眾願意投稿題目，且品質可控。

**前置條件**：v3 上線後有人主動 DM 問「我能不能投稿一題」。

### 新增功能

#### 1. LLM 出題助手
- `/turtle_soup_create` 互動式：
  1. 玩家口述湯底
  2. LLM 提取 key_facts
  3. LLM 反向生成湯面（隱藏關鍵資訊但保留邏輯線索）
  4. LLM 生成 5-10 個 verified_qa（玩家審核 / 修正）
- 玩家 confirm → 題目存入 pending pool

#### 2. 題目審核
- `/turtle_soup_review` — Marvin team 或可信玩家審核 pending pool
- 自動跑 sanity check：湯面 ≠ 湯底、key_facts 邏輯一致、verified_qa 與 truth 一致
- 通過 → 進正式題庫

#### 3. 投稿者署名與激勵
- 題庫 entry 加 `author: <user_id>`
- 玩家玩到別人投的題會看到「by Showay」
- 排行榜：「最熱門題目作者」（v3 計分機制延伸）

#### 4. 玩家評分
- 玩完一題可給 1-5 星
- 題庫定期下架低分題（< 3 星 + 玩過 ≥ 10 次）

### v4 驗收

- [ ] 累積投稿 ≥ 50 題
- [ ] 審核通過率 ≥ 60%（代表自動生成品質可用）
- [ ] 最熱門 UGC 題目單週被玩 ≥ 20 次
- [ ] 玩家平均評分 ≥ 3.5 星

---

## v5: Streamer 個人化資產

**驗證假設**：海龜湯能變成 streamer 的個人化、可商業化的內容資產。

**前置條件**：v4 跑滿三個月後 streamer 開始討論「能不能用我的聲音當主持」。

### 新增功能

#### 1. Streamer Voice Clone 整合
- 沿 Marvin 既有 VOD voice clone 基建
- streamer 上傳 VOD → 訓練 voice → 在他們的頻道玩海龜湯時用他們的聲音當主持人
- 「Marvin」變成 streamer 的私人化身

#### 2. 從 VOD 抽題
- streamer 上傳直播 VOD
- LLM 分析 highlight 段落（如：streamer 講了某個故事 / 笑話）
- LLM 自動生成基於該故事的海龜湯題目
- streamer 審核後加入私有題庫

#### 3. 私人題庫 / 訂閱題庫
- streamer 自己的題庫（觀眾僅在他頻道能玩）
- 跨 streamer 訂閱機制（A streamer 訂閱 B streamer 的題庫）

#### 4. 商業化掛勾
- streamer subscriber tier 解鎖私人題庫
- 觀眾 super chat 觸發特殊題目 / hint

### v5 驗收

- [ ] 至少 3 個 streamer 使用 voice clone 主持
- [ ] VOD 自動出題通過率 ≥ 30%
- [ ] 訂閱題庫機制有真實使用案例
- [ ] 至少 1 個 streamer 把海龜湯訂閱收入計入頻道營收

---

## 跨版本基建演進

每升版要從遊戲特定的程式碼提取到 `game/common/` 的共用模組。

| 版本 | 抽出到 common | 目的 |
|---|---|---|
| v0 → v1 | nothing | 還沒夠多重複，太早抽會錯 |
| v1 → v2 | `llm_judge_base.py`（3-layer fallback wrapper）| 給狼人殺等未來遊戲共用 |
| v2 → v3 | `scoring_engine.py`（共用計分邏輯）、`tournament.py`（連續遊戲流程）| Busted99 / 海龜湯 / 狼人殺都會用 |
| v3 → v4 | `twitch_overlay.py`（chat 同步 + 觀眾互動）、`ugc_pipeline.py`（玩家投稿審核流程）| 跨遊戲 |
| v4 → v5 | `voice_persona.py`（streamer voice clone 管理）| 跨遊戲 |

抽離規則：**至少有兩個遊戲在用，才開始抽**。Premature abstraction 比 duplication 貴。

---

## 何時開始狼人殺？

不晚於 **v2 完成後**。理由：
- v2 跑通 = STT 重度路徑、題庫管理、多 LLM judge 並發、SFX/TTS 序列都驗證過
- 共用基建已抽出 `llm_judge_base`、`scenario_bank`
- 狼人殺可以基於這層基建快速搭建，省 30-40% 工時

狼人殺自己也會有 v0 / v1 / v2 演進，當下不規劃太多——等海龜湯 v2 完成時再寫狼人殺的 REQUIREMENTS.md。

---

## 取消 / 重新規劃條件

任何版本驗收失敗時，**先停下重評估假設**，不是直接開下一版：

- v0 失敗（STT/LLM loop 不能跑）→ 整個海龜湯方向取消，回去做別的遊戲
- v1 失敗（玩家覺得無聊）→ 重新設計 verdict / Marvin 人格 / hint 系統，或砍掉部分功能
- v2 失敗（玩家不會回來）→ 可能海龜湯天然就是「玩過一次就懂了」，考慮把它變成「每週一題」而非常駐
- v3 失敗（streamer 不買單）→ 改走 B2C，不走 streamer 方向
- v4 失敗（UGC 品質不行）→ 退回專業策劃（內部團隊出題）
- v5 失敗（voice clone 不夠像）→ 等 voice clone tech 成熟再來
