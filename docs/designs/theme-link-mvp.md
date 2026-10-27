# 主題連結 MVP 規格（只放歌、不開口）

狀態：DRAFT（2026-10-27）。上游設計：[song-life-story.md](song-life-story.md)。

## 一句話
房間 14 天內在不同日子裡一再聊到的話題（10 分鐘日誌的 meme tag），由 Marvin 在 autopilot 裡挑一首「故事層」呼應它的歌來放。不解釋、不開口，懂的人自己懂。

## 不做的事（本期範圍外）
- 不講創作背景（第 5 項開口、付費 grounding 升級都延後）。
- 不做 tag 同義合併（只做字面正規化）。
- 不讀 events（個人事件）。
- 不新增曲庫外的歌：只從 music_memory 已有的歌裡挑。

## 架構：離線批次算好，執行時併進既有的 Associative 呼叫
語音熱路徑不新增 LLM 呼叫；沿用 Associative 原本那一次（付費、20 秒逾時）。

### A. 離線批次 `scripts/build_theme_links.py`（手動或 launchd 每天跑一次）
全部 LLM 呼叫走 bus：`build_tiered_router(env={"GOOGLE_API_KEY": ...})`（只放 Gemini 免費 key → 池裡只有 gemini_free/gemini_free_25，沒有付費兜底，也不會掉到 Groq 等對華語歌不熟的模型），用 `analyze` tier、`json=True`。

1. **歌卡**（表 `song_cards`）
   - 來源：`music_memory.json` 的歌，`total_plays >= 2`，依 total_plays 由高到低，跳過已有卡的。每晚最多 `CARD_BATCH_LIMIT = 150` 張。
   - 主鍵 `video_id`（`extract_video_id(webpage_url)`；取不到就跳過）。
   - prompt：沿用盲測 `card_free.py` 的 CARD prompt（憑知識，不熟就寫「不確定」）。欄位：`imagery`、`deep_story`、`relation`、`weight`（輕|中|重）、`familiarity`（高|中|低）、`created_at`。
   - 解析失敗、`deep_story` 是「不確定」的，存一筆 `familiarity='低'`、`deep_story=''`，避免每晚重打同一首。
2. **tag 收集**（純函式，不打 LLM）
   - `chat_summary_parser.parse_log(records/chat_summary_log.txt)`，取近 14 天、`meme_id` 非空的條目。
   - 正規化：去空白；用「、」「/」「，」拆成多個 tag；丟掉長度 < 2 的。
   - 合格 tag：在 ≥ 2 個不同日期出現。
3. **heard_by**（純函式）
   - 每個帶這個 tag 的窗：時間 = 日誌時間戳（台灣時間）往前 10 分鐘到該時間戳。
   - 從 `data/voice_presence.jsonl` 的 join/leave 事件，重建「這段時間內任何時刻在語音頻道的非 bot 使用者」（用 `user_name`）。
   - heard_by = 所有窗的在場者聯集。
   - 窗內重建不出任何人 → 這個窗不算數。
4. **tag 釋義＋敏感**（表 `theme_tags`；每個 tag 只算一次，`last_seen` 有更新才重算 heard_by）
   - LLM 輸出：`meaning`（字典本義＋延伸義，一兩句；例如「旅行＝離開熟悉的地方；延伸：異地、出走、告別」）、`sensitive`（bool：健康、喪親、感情、衝突、金錢糾紛）。
   - `sensitive` 缺漏或非 bool → 視為 True。
5. **房間把關**（同表欄位 `room_veto`）
   - 取該 tag 最近一個窗前 15 分鐘的 `marvin.db transcripts`（排除 speaker=Marvin）。
   - < 5 行 → `room_veto = NULL`（不判斷，不擋）。
   - ≥ 5 行 → LLM 判斷「這段對話是否觸及健康、喪親、感情、衝突等敏感事」→ `room_veto = 1/0`；解析失敗視為 1。
   - 逐字稿只在批次的記憶體中使用，**不存原文，也不存 LLM 的判讀理由**。
6. **配歌**（表 `theme_picks`）
   - 只對 `sensitive=0` 且 `room_veto` 不是 1 的 tag 做。
   - 候選：有卡、`familiarity in ('高','中')`、`weight != '重'` 的歌，依 total_plays 取前 `MATCH_POOL = 120` 張。
   - 候選為空 → 不呼叫 LLM（盲測：空候選會被 LLM 自己編歌）。
   - LLM 拿 tag 的 meaning 和編號過的歌卡（imagery＋deep_story），回傳最多 5 個**編號**和層別（意象|深層|兩者）。編號超出範圍就丟掉（不准 LLM 自己寫歌名）。
   - 存 `(tag, video_id, rank, layer)`。tag 的 meaning 變了就重算。

### B. 執行時：併進既有的 `_try_associative_pick`（不新增入口）
2026-10-27 使用者定：不另開 `_try_theme_pick`，避免兩套冷卻／去重／品質閘打地鼠。主題連結是 AssociativeCuration 的**第二種素材**，共用它的冷卻、30 天不重複、解析、品質閘、入隊、`associative_picks.jsonl`。批次第 6 步的 `theme_picks` 改為**粗篩**（每個 tag 預選 5 首），最後由 Associative 那一次 LLM 呼叫決定。

`cogs/music_cog_autopilot.py::_try_associative_pick` 的改動：
1. **合格 tag**（在既有冷卻閘門通過之後算；env `THEME_LINK` 預設 on，off/0/false/no 時跳過這段，行為與現在完全相同）：
   - `theme_tags` 中 `last_seen` 在 14 天內、`sensitive=0`、`room_veto` 不是 1。
   - 目前在場成員每一個都 ∈ heard_by（members 空 → 沒有合格 tag）。
   - `associative_picks.jsonl` 裡 14 天內沒有 `tag == 這個 tag` 的紀錄。
   - 今天（台灣時間）已經有一筆 `source == "tag"` 的紀錄 → 這次不給任何 tag。
   - 依 last_seen 新到舊，最多 3 個。
   - 沒有合格 tag → 照舊只用對話。**對話不足 2 句、但有合格 tag 時也可以呼叫**（原本 `< 2` 句就 return 0，改成「對話 < 2 句且沒有合格 tag」才 return 0）。
2. **本地歌單加料**：
   - 合格 tag 的 `theme_picks`（依 rank）解成 music_memory entry，經同一組排除（`exclude_titles`、skipped、7 天內播過），**排在 `local_cands` 最前面**、去重；總數仍 ≤ `LOCAL_POOL_LIMIT`（40），超過時從原本播放數排序的尾巴砍。
   - 給 LLM 看的每一行：有歌卡時 `編號. 歌手 - 歌名｜意象：…｜故事：…`（各截 40 字），沒有卡時照舊。
3. **prompt**（`associative_curation.build_associative_prompt` 新增選填參數 `themes: list[tuple[str, str]]`＝[(tag, meaning)]，不傳＝prompt 完全不變）：
   - 加一段【最近大家反覆聊到的話題（Marvin 記得的事；選了也不准講出來）】列出 `tag：meaning`。
   - 規則加一條：每首 pick 加 `source` 欄，值為 `now` 或 `tag:<話題>`；`source=tag` 的歌**必須來自本地歌單（填 local）**，依「故事層」呼應話題的釋義，不准只靠字面。
4. **解析**（`parse_associative_picks` 新增選填 `allowed_tags: set[str]`）：
   - `AssociativePick` 加欄位 `source: str = "now"`、`tag: str = ""`。
   - `source` 是 `tag:X` 且 X ∈ allowed_tags 且 local_index 不是 None → `source="tag"`、`tag=X`；`tag:X` 但條件不符 → 丟掉這首；其他值或缺漏 → `"now"`。
5. **入隊**（沿用原流程，只改 tag 來源的三處）：
   - `source=="tag"`：`_dj_line = ""`（不開口）、`requested_by = "Marvin推薦（點給大家）"`、`info['_lane'] = 'theme'`、`info['_theme_tag'] = tag`。
   - `append_pick` 加選填 `source`、`tag` 兩個 keyword，寫進 jsonl；舊呼叫不帶 = 不寫這兩欄。
   - log 沿用 `🎵 [AssociativePick] 成功入隊`，句尾加 `source=tag:露營`。
   - 合格 tag 數、被哪個閘門擋下（在場、14 天內用過、今天已用）各 log 一行 INFO，前綴 `🧭 [ThemeLink]`。

### C. 表結構（`marvin.db`，批次腳本負責 `CREATE TABLE IF NOT EXISTS`）
```sql
song_cards(video_id TEXT PRIMARY KEY, artist TEXT, title TEXT, imagery TEXT, deep_story TEXT,
           relation TEXT, weight TEXT, familiarity TEXT, created_at REAL)
theme_tags(tag TEXT PRIMARY KEY, meaning TEXT, sensitive INTEGER, room_veto INTEGER,
           heard_by TEXT /* JSON list */, first_seen REAL, last_seen REAL, day_count INTEGER, updated_at REAL)
theme_picks(tag TEXT, video_id TEXT, rank INTEGER, layer TEXT, PRIMARY KEY(tag, video_id))
```
執行時只讀這三張表（唯讀連線），不寫 DB。批次第 6 步產生的 `theme_picks` 是粗篩結果，最後挑哪首由執行時的 Associative 決定。

## 模組切分
- `theme_link.py`（新，頂層）：純函式 —— tag 正規化、合格 tag、presence 重建 heard_by、在場閘門、LLM 輸出解析（card / meaning / picks，含編號範圍檢查、sensitive 預設 True）、DB 讀寫小函式。
- `scripts/build_theme_links.py`（新）：批次的 IO 外殼。
- `associative_curation.py`：`build_associative_prompt(themes=…)`、`parse_associative_picks(allowed_tags=…)`、`AssociativePick.source/tag`、本地歌單行可帶歌卡。
- `associative_history.py`：`append_pick(source=, tag=)`；新增讀取「14 天內用過的 tag」「今天是否已有 source=tag」的函式。
- `cogs/music_cog_autopilot.py`：`_try_associative_pick` 依上面 B 段改。
- `cogs/music_cog_story_arc.py`：不改。
- 新增頂層模組有 INFO log → `main_discord.setup_early_logging` 加 `logging.getLogger("theme_link").setLevel(logging.INFO)`，並補 `tests/test_main_discord_logging.py`。

## 驗收（pytest，可機器判斷）
1. 正規化：「搬家、延期」→ [搬家, 延期]；「AI 剪輯」→ AI剪輯；「-」→ []。
2. 合格 tag：同一天出現 3 次 → 不合格；兩個不同日期 → 合格；超過 14 天的不算。
3. heard_by：join 在窗前、leave 在窗後 → 在場；leave 在窗開始前 → 不在場；窗內 join → 在場；bot 不算。
4. 在場閘門：members ⊆ heard_by 才過；有人不在 heard_by → 不過；members 空 → 不過。
5. 批次解析：picks 編號超出範圍會被丟掉；sensitive 缺漏或是字串 → True；card 解析失敗 → familiarity 低；配歌候選為空時，假 LLM 不會被呼叫。
6. `build_associative_prompt` 不傳 themes → 輸出與改動前**逐字相同**（用改動前的固定輸入比對字串）。
7. `parse_associative_picks`：`source=tag:露營` 且在 allowed_tags、有 local → source=tag；不在 allowed_tags 或沒有 local → 該首被丟掉；缺 source → now。
8. `_try_associative_pick`：
   - THEME_LINK=off → 不讀 theme 表，prompt 不含話題段。
   - 今天已有 source=tag 的紀錄 → 不給 tag；tag 14 天內用過 → 不給；在場有人不在 heard_by → 不給。
   - 對話 0 句但有合格 tag → 仍會呼叫 LLM；對話 0 句且沒有合格 tag → 回 0、不呼叫。
   - LLM 回 source=tag 的 pick → 入隊的 `_dj_line == ""`、`requested_by == "Marvin推薦（點給大家）"`、`_lane == "theme"`，jsonl 有 source/tag。
   - LLM 回 source=now → 行為與改動前相同（requested_by「對話靈感」、dj_line 保留）。
9. theme 的 picks 會排在 local_cands 最前面，總數不超過 40。
10. 全套測試綠；既有 Associative 測試不需要修改就要全過（若必須改，停下回報）。

## 上線後觀察（一週）
- grep `🧭 [ThemeLink]`：觸發次數、tag、選到的歌、層別；閘門擋下的分布。
- 使用者主觀：選得對不對、有沒有「它怎麼知道」。
- 跳過率：`_lane == 'theme'` 的歌 vs 整體 3.5%、vs Associative 的 0.9%。
- `associative_picks.jsonl` 的 source 分布：tag 是否幾乎選不到（LLM 偏好 now）。
- 付費帳本 `associative_curation` 每日花費有沒有明顯上升（預估每次多約 1.5k token）。
