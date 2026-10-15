# TTS 節奏與呼吸感改善 — 交辦規格

> 目標：讓 Marvin 的 edge-tts 輸出有人類說話的斷點與呼吸感。
> edge-tts 只吃 voice/rate/pitch/volume（微軟擋掉自訂 SSML，無 `<break>`），
> **節奏幾乎完全由送進去的文字（標點）決定**，所以本計畫只動「文字」這一層。
>
> 執行原則：照本文件做，**設計已定案，不要自行發揮**。遇到本文件沒寫到、
> 或實際程式碼跟本文件描述不符的情況，**停下來回報，不要猜**。
> 不順手重構、不清無關死碼。

---

## 背景：已確認的問題

`tts_engine.py` 的 `SukiTTS._clean_text()`（約 L193–242）在送 edge-tts 前會清洗文字。
第 235 行的白名單 regex 只保留 `中日文、英數、,.!?;:"'-、空白`，導致最能表達呼吸感的標點被**整個刪掉**：

| LLM 寫的 | 實際送進 edge-tts | 問題 |
|---|---|---|
| `我想想⋯⋯好吧` | `我想想好吧` | `⋯`（U+22EF，台灣輸入法預設的刪節號）被刪，停頓消失 |
| `欸——你說什麼` | `欸你說什麼` | 破折號 `—`（U+2014）被刪 |
| `好啦～走吧` | `好啦走吧` | `～` 被 NFKC 轉成 `~` 再被刪，前後黏在一起 |
| `嗯…其實，我不知道。` | `嗯。。。其實,我不知道.` | ✅ 正常（`…` 經 NFKC 變 `...` 再轉 `。。。`） |

（以上為實際抽出 `_clean_text` 執行的結果。）

另有兩個**未經驗證**的既有假設，本次只量測、不改：
- L216 註解說「全形標點轉半形可優化 Edge TTS 停頓感」——對 zh-TW 聲音是否成立沒有證據。
- `。。。` 是否真的比 `，` 產生更長停頓——沒有量過。

---

## Phase 1：修 `_clean_text` 吃掉停頓標點（TDD）

### 改動（只動 `tts_engine.py` 的 `_clean_text`）

在 **step 4 之後、step 5 的 `unicodedata.normalize('NFKC', ...)` 之前**，插入一步：

```python
# 4.5 停頓標點保留：白名單 regex 會刪掉 ⋯ / 破折號 / 波浪號，先換成會產生停頓的等價標點
cleaned = cleaned.replace("⋯", "…")              # U+22EF → U+2026，之後沿用既有 … → 。。。 路徑
cleaned = re.sub(r"[—―─]+", "，", cleaned)        # 破折號（U+2014/U+2015/U+2500，連續視為一個）→ 逗號
cleaned = re.sub(r"[～~]+", "，", cleaned)         # 波浪號（全形/半形，連續視為一個）→ 逗號
```

- 換成全形 `，` 是刻意的：後面既有的 punctuation_map 會再把它轉成 `,`，行為跟其他逗號一致。
- `⋯⋯` 會變成 `……` → NFKC `......` → `。。。。。。`，跟現在 `……` 的行為完全一致（不另做合併）。
- **不要**改 punctuation_map、白名單 regex、`。。。` 注入邏輯、`get_estimated_duration`。

### 測試：新增 `tests/test_tts_clean_text_pauses.py`

照 `tests/test_tts_azure_fallback.py` 建立 `SukiTTS` 實例的方式取得 `tts`，直接呼叫 `tts._clean_text(...)`。
**先寫測試、確認紅，再改實作。** 期望值（逐字比對）：

| 輸入 | 期望輸出 |
|---|---|
| `我想想⋯⋯好吧` | `我想想。。。。。。好吧` |
| `我想想⋯好吧` | `我想想。。。好吧` |
| `欸——你說什麼` | `欸,你說什麼` |
| `欸—你說什麼` | `欸,你說什麼` |
| `好啦～走吧` | `好啦,走吧` |
| `好啦~~走吧` | `好啦,走吧` |
| `嗯…其實，我不知道。`（回歸） | `嗯。。。其實,我不知道.` |
| `這首歌，嗯，還不錯吧？`（回歸） | `這首歌,嗯,還不錯吧?` |

測試檔內**不准**複製 `_clean_text` 的邏輯來算期望值，期望值一律寫死字串。

### 驗收
1. 新測試改實作前紅、改後綠（回報兩次輸出）。
2. `pytest tests/test_tts_*.py tests/test_music_fastpath_stutter.py tests/test_dj_narration_snapshots.py` 全綠。
3. Mutation check：分別註解掉三行新增中的任一行，對應測試要紅。

---

## Phase 2：停頓量測腳本（回答「哪種標點真的有停頓」）

### 新增 `scripts/measure_tts_pauses.py`

- 用 `edge_tts.Communicate(text, voice="zh-TW-YunJheNeural", rate="-20%", pitch="-15Hz")`
  （與 `SukiTTS` 預設相同）合成，**直接送原始字串、不經 `_clean_text`**——目的是量「標點本身」的效果。
- 語料寫死在腳本內：3 個句框 × 8 種標點變體 = 24 句。
  - 句框：`我想想{P}好吧`、`這首歌{P}其實是他二十歲寫的`、`欸{P}你有沒有聽到`
  - `{P}` 變體：`""`、`"，"`、`","`、`"。"`、`"."`、`"。。。"`、`"。。。。。。"`、`" "`（半形空白）
- 每句存成 `<out_dir>/<frame_idx>_<variant_idx>.mp3`（`--out` 參數，預設 `./tts_pause_out`），供人耳試聽。
- 停頓偵測：`ffmpeg -i <mp3> -af silencedetect=noise=-40dB:d=0.08 -f null -`，解析 stderr 的 `silence_start` / `silence_duration`。
  **排除**開頭（start ≤ 0.05s）與結尾（延伸到檔尾 0.05s 內）的靜音，只留句中停頓。
- 解析邏輯寫成獨立純函式 `parse_silences(stderr: str, total_dur: float) -> list[float]`，
  總長度用 `ffprobe -show_entries format=duration` 取得。
- 輸出 markdown 表到 stdout：`句框 | 變體 | 總長(s) | 句中停頓(s)`。
- 腳本需要連網，在本機（Mac）跑，不在 CI 跑。不加新的 pip 依賴。

### 測試：`tests/test_measure_tts_pauses.py`
只測 `parse_silences`：餵一段寫死的 silencedetect stderr 樣本（含開頭靜音、句中兩段、結尾靜音），
斷言只回傳句中兩段的 duration。不呼叫網路、不呼叫 ffmpeg。

### 驗收
1. `parse_silences` 測試綠。
2. 交付後由人在本機跑一次，把表格貼回來——**全形/半形、`。。。` 是否有效的結論由人看表決定，本 Phase 不改任何既有行為。**

---

## Phase 3：DJ 口白 prompt 加口語節奏規則

範圍**只限 DJ 組**（`gemini_router_content.py` 約 L938 的 `_DJ_EVENT_TYPES`，is_dj 分支）。
馬文聊天 / 其他 SPOKEN_PURPOSES 本次不動（各自有字數、押韻、格式約束，要個別評估）。

### 改動
1. 在 `gemini_router_content.py` 模組層級新增常數（文字逐字照抄，不要潤飾）：

```python
# 口白會被 edge-tts 念出來，節奏完全由標點決定（edge 不吃 SSML break）。見 docs/tts_breath_plan.md
SPOKEN_RHYTHM_RULES = (
    "【口語節奏】這段會被念出來：用短句，大約每 4～10 字用逗號換一口氣；"
    "想停頓、遲疑或轉折時用「⋯⋯」；可以用「嗯、欸、就是說」起頭或接話，但整段最多一個；"
    "不要用括號、引號、條列。"
)
```

2. is_dj 分支組 `sys_prompt` 時接在最後：
   `sys_prompt = f"你是 DJ Marvin，{_dj_role}。任務：{prompts[event_type]}\n{SPOKEN_RHYTHM_RULES}"`
   非 DJ 分支不動。

### 動手前先確認（任一不符就停下回報）
- `_is_qualified_dj_script`（`dj_prompt_builder` / `cogs/music_cog_dj_lyrics.py` 一帶）不會因為文字含 `⋯⋯`、多個逗號、或「嗯/欸」開頭而判不合格。
- `tts_length_policy.py` 的截斷在 `⋯` 處切斷後，Phase 1 修正後的 `_clean_text` 能正確轉出停頓（`⋯` 已列在 `_SENTENCE_END_CHARS`）。

### 測試
- 新增測試：mock `_call_llm`，觸發一次 `dj_interjection`，斷言傳給 LLM 的 prompt 含 `SPOKEN_RHYTHM_RULES`；
  觸發一個非 DJ event_type，斷言**不含**。
- 若 `_is_qualified_dj_script` 可直接呼叫，加一個 case：`"嗯，這首歌⋯⋯其實是他二十歲寫的，很有意思。"` 判定合格。

### 驗收
1. 新測試綠；`tests/test_dj_narration_snapshots.py` **不需更新 golden 就全綠**（快照拍的是 context 不是 system prompt；若需要更新，停下回報原因）。
2. 全套 `pytest` 無新增失敗。

---

## 不在本次範圍（Phase 4 候選，等 Phase 2 量測後由人決定）

**自製停頓：分段合成 + 插入靜音。** 在句尾與 `⋯⋯` 處切段、各段分別合成，
段間插入 150–400ms 隨機長度靜音（長停頓處可選擇加真人呼吸取樣）。
這是 edge-tts 上唯一能精確控制停頓長度的方法，但：
- 每段結尾會被念成句尾降調，切太細會像一句一句念稿 → 只在句尾切、逗號不切；
- edge 呼叫次數倍增，會碰到既有限流/斷路器（`_edge_voice_down`、storm fallback）；
- 動的是 TTS 骨幹（三條分支共用），fallback 鏈（edge → secondary → Azure 首段切分 → say）都要一起考慮。
**本次不要實作。**

---

## 交付回報格式
每個 Phase 完成時回報：改了哪些檔（`git diff --stat`）、測試指令與輸出、mutation check 結果、
任何與本文件描述不符而停下來的點。

---

## 實作結果與量測（2026-10-15）

Phase 1、2 照上面做完；Phase 3 在量測後改了內容（見下）。

### 量測（zh-TW-YunJheNeural, rate -20%, pitch -15Hz，句中停頓秒數）
| 標點 | 停頓 |
|---|---|
| 無 / 半形空白 / 引號書名號 | 0 |
| `，,；;：:、` 與半形 `.?!` | 0.3–0.6 |
| 全形 `。？！` | 1.4–1.7 |
| `。。。`（≒原本的 `…`） | 1.8–1.9；`。。。。。。` 不會更長 |

- 句尾不論有無標點、全形半形，edge 都固定補約 1.25s 尾端靜音。
- 句首/句中助詞（嗯、欸、喔、啊）的停頓來自後面的逗號（≈0.4s）；句尾「啦」「耶」總長沒變，疑似被吞，未用耳朵確認。
- edge 約每 16–20 次隨機回 NoAudioReceived，同句重送即可。

**結論：L216 註解「全形轉半形優化停頓」不成立**。`。→.`、NFKC `！？→!?` 把句間停頓從 1.5s 砍到 0.4s。

### 定案（使用者 10/15 拍板）
1. 全形 `。！？` 保留，半形 `!?` 轉全形。
2. 停頓上限 1.5s：刪節號 → 單一 `。`，連續長停頓只留第一個。1.5s 本身先上線實聽，嫌長再做 Phase 4。
3. 防一路講到底：>14 字無標點片段，只在 `》` 後或強連接詞前補逗號，找不到就不動（在 2,676 句實際口白上試過，`「」『』` 後補常切錯，故排除）。
4. `get_estimated_duration` 計入句中長停頓（每個 1.25×1.2s）。
5. DJ 口白 prompt 只加「每句不超過 15 個字，用逗號或句號斷開」，不給任何字面範例（原 Phase 3 的「嗯、欸、⋯⋯」範例會被照抄）。

### 已知缺口
`tts_length_policy._budget_chars` 用無標點樣本換算字數預算，截斷後句中每多一個句號實際仍可能超時長上限約 1.5s。
