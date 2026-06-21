# 漫畫產生器設計參考（日式分鏡 + 表情 prompt）

2026-06-21 上網研究後整理。對照 `diary_comic/` 現況，標出已做 ✅ / 待改 ⚠️ / 新點子 💡。
來源見文末。

---

## A. 日式分鏡核心原則

### 1. 切格大小 = 強弱與節奏（最重要）
- **一頁一個最大、最重要的「格/塊」≥40% 面積**（Jack 2026-06-21）。我們的高潮 = Hero 斜切 duo 整塊。
- 先決定高潮點（爆笑那一幕），全頁 40%+ 留給它；**其餘格都為它鋪陳**。
- 現代漫畫（火影/海賊）約 3-4 格/頁，emphasis 驅動，不是塞滿。
- ✅ 已做：story 路徑 = `compose_page_hero`，Hero **斜切 duo**（heat 高 → 自動主導 ≥40%）；物件 context 小格在上。
  （另有 `splash_layout`/`compose_splash_page` 矩形大砸框版 + 「垂直窄/水平寬」鐵律，備用，story 不走 —— Jack 要 B 斜切版。）

### 2. Gutter（格間距）編碼「時間」
- 橫向 gutter > 縱向 gutter。
- **窄 gutter = 同時 / 連續 / 快**；**寬 gutter = 時間流逝 / 留白思考**。
- ⚠️ 待改 #1：目前 gutter 均勻。可改成「同場景 beat 之間窄、跳話題之間寬」。

### 3. 斜格 / 傾斜分鏡 = 張力 / 動作 / 速度（選擇性，非每格）
- 對角切 = drama；圓弧格 = 平靜。
- **Broken border**（角色破格衝出邊框）= 強烈動感。
- ✅ 已做：只有 Hero 斜切（`hero_split_polys` / `compose_page_hero`）。
- ⚠️ 待改 #2：Hero 角色可破格衝出。

### 4b. 同源裁切（一張高清素材裁多格）— Jack 2026-06-21
- 一張（**2K** `gemini-3-pro-image`）鋪陳場景 → 裁多格。省 API、角色**零飄移**。
- ⚠️ 裁愈緊愈糊：768px nano 裁特寫(245px)會糊 → **特寫一定要 2K 素材**（中景 65% 還 OK）。
- ✅ 已做：`crops_from_source(src, specs)` / `split_lr_specs(ratio)`（遠景精準對切左右）/ `pushin_specs()`。

**定案結構（Jack）：格1 焦點+全景 + 格2 中景 + 格3 Hero斜切duo**
- **格1 = B 打法 `zoom_wide_specs`**（定案）：左=講者放大特寫(焦點)、右=全景(脈絡)，`pair` row（左窄右寬）。
  左格放大「**講笑話的那個人**」當情緒錨點。⚠️ 左格放大需 **2K 源**才不糊。
- 備案 A `split_lr_specs`：左右**精準對切兩個不同主體**（零重疊）；源圖需構圖成「左主體+右主體」。B 預設、A 看情況。
- ⚠️ 待接：render_story `_render_slant` 改 `zoom_wide(格1) + 中景(格2) + duo(格3)`（等額度，需 2K 出圖）。

### 4. 鏡頭變化 — 三距離節奏（避免每格證件照）
- **遠景 Wide**：交代環境/空間（每頁第一格）。
- **中景 Medium**：角色動作、肢體語言、互動。
- **特寫 Close-up**：放大眼神/嘴角/道具，傳達強烈情緒。
- 三者**交替**（不連三同距），高潮用特寫推情緒。rule of thirds、極特寫=親密、遠景=場景。
- ✅ 已做：`camera.py::shot_for` 走 `_RHYTHM`（遠→中↔特，定期回遠 re-establish）；
  Hero=情緒特寫；池 `_WIDE`/`_MEDIUM`/`_CLOSEUP`（含 extreme close-up、silhouette、broken-frame）。

### 5. 對白氣泡
- **鐵則：絕不蓋臉、不蓋關鍵動作 / 細節**。
- 閱讀順序：日漫右上先讀（右到左）；多氣泡沿「閱讀對角線」、靠近=同組、先講的做大。
- 四角配置別等距：前兩個靠近、第三四個拉開。
- ✅ 已做：`_busyness` 自動挑最乾淨角落避臉。
- 註：我們每格單一 caption，多氣泡排序用不到；位置可依閱讀流（先講→上方）。

### 6. 其他效果
- Overlapping panels / broken border = 快速連續動作。
- 波浪/模糊邊框 = 回憶 / 倒敘。
- **Inset panel**（大格上疊小格）= 隔離強調某反應。
- 💡 待改 #3：哄堂笑的「反應特寫」用 inset 小格疊在主場景上（最適合精華漫畫的笑點）。

---

## B. 表情豐富的 prompt 技巧

研究結論：**表情靠「明確情緒 + 三個臉部元件（眉/眼/嘴）+ 視線方向」堆出來，動漫風要誇張**。

### 寫法
1. **明確命名情緒**，別只說 happy：用「wide-eyed delight / smug grin / exhausted deadpan / embarrassed cringe」。
2. **拆三元件**：
   - 眉：raised / furrowed / one eyebrow up。
   - 眼：wide / squinting / sparkling / half-lidded；**加視線方向**（looking away, side-eye）。
   - 嘴：open-mouth laugh / smirk / gritted teeth / pursed。
3. **動漫誇張符號**：sweat drop（汗滴）、blush lines（臉紅線）、shock lines（驚嚇線）、popping veins（青筋）、chibi-fy（Q 版化）做喜劇。
4. **逗號分隔關鍵詞**：`grinning wide, eyes squeezed shut, head thrown back laughing`。
5. **每個角色給不同情緒**：笑的人=delighted、被虧的人=embarrassed → 對比才有戲。
6. **情緒可疊**：複合表情更有層次（如「強忍笑意 = 抿嘴+肩抖」）。

### 套進 `build_panel_prompt`（已實作）
全域 style 加：角色要有**大、誇張、隨場景變化的表情**（wide eyes / 眉毛戲 / 開口笑 / 汗滴 / 臉紅），清楚讀得出當下情緒。Hero/punchline 格 → 最誇張的反應。
精華漫畫（笑點）特別吃這個：哄堂笑那格要畫出真正爆笑的臉。

---

## 4 個分鏡改進（2026-06-21 程式已寫好，等額度回來生效）
1. ✅ **變動 gutter** — `layout.gutter_between(prev, next, base)`：相鄰格相似→窄、跳主題→寬。已接進 `compose_page_webtoon`。
2. ✅ **Hero 角色破格** — `camera._HERO` 加 prompt cue「bursting out of the frame, broken-border energy」。
3. ✅ **Inset 反應特寫** — `layout.paste_inset` + `Panel.inset` 欄位 + 已接 webtoon（角落疊小格）。
   ⚠️ 還缺：`render_session` 要 populate `panel.inset`（生一張反應特寫圖塞進去）—— 這步要 API，等額度回來接。
4. ✅ **鏡頭詞彙擴充** — `camera._SHOTS` 加 extreme close-up / silhouette / broken-frame。

✅ **B 表情 prompt** — 已做進 `build_panel_prompt` 全域 style（大、誇張、每角色不同表情）。

**全部需要 API 出圖才看得到效果**（現撞月支出上限）。等額度回來，新生的漫畫就會套上：豐富表情 + 變動節奏 + Hero 破格 + 更多鏡頭；inset 反應特寫再補最後的 render 串接。

---

## C. 故事編排：精華 + 日誌融合（2026-06-21 定案）

**比喻**：10 分鐘日誌 = 故事骨幹（arc），精華（爆笑）= 高潮峰值。用時間軸 join。

**路由（`story.choose_format`）**：
```
有爆笑精華嗎？
├─ 沒 → 不出（沒高潮不畫）
└─ 有：
   ├─ 豐富（≥6筆context）→ 日漫 4 格
   └─ 薄 → 一格 meme
（韓國條漫先 off — 信心不足）
```

**日漫故事模板（Hero 斜切 duo —— Jack 定案 B）**：
```
標題bar（LLM 單話名「今晚精華：足球烏龍」）
┌ 物件 context（鋪陳，方正小格，冷）┐
┌ 物件 context（鋪陳，方正小格，冷）┐
┌ ★ Hero 斜切 duo（heat高→主導 ≥40%）┐
│ ╱ 上格 = 鋪哏 setup（中景，那人講）  │
│╱  下格 = 爆笑 reaction（情緒特寫，熱）│ ← 斜切拆兩拍
└────────────────────────┘
```
- **一頁一個大塊 = duo 整塊 ≥40%**（heat 高自動主導），context 小格純鋪陳。
- 拆兩拍（Ⓐ）：精華 setup→punchline 兩拍，**斜切分隔**（規則3：動態用斜框）。
- 鋪陳方正、高潮斜切 = 方圓對比、一冷一熱。
- arc（②B）：context 在上鋪陳、高潮 duo 在底，往下讀到爆點。

**樣板輪替（4 版，避免每天同一版面）—— `story.choose_template(plan, day_index)`**：
共用鐵則不變（一頁一個 Hero 大塊 ≥40%、三距離、鋪陳方正/Hero斜切）；變 Hero 位置 + 鋪陳排法。
| id | 結構（row 序）| Hero | 個性 |
|---|---|---|---|
| T1 建勢底爆 | pair(焦點\|全景) → 中景 → **duo** | 底 | 正常建勢（穩） |
| T2 頂爆倒敘 | **duo** → 中景 → pair(全景\|焦點) | 頂 | 先爆當鉤子再倒敘（衝） |
| T3 純方正三拍 | 遠景 → 中景 → **duo** | 底 | 鋪陳全方正、冷熱對比最強（沉穩） |
| T4 中央爆+余韵 | 遠景 → **duo** → pair(反應A\|反應B) | 中 | 爆點夾中、底部余韵收尾（後勁） |
- **挑版邏輯**：內容分層 → 層內日期輪。夠強(不需馬文救援)→衝池 `(T2,T4)`；普通→穩池 `(T1,T3)`；`day_index%2` 層內輪。
- **長寬比鎖定（Jack 2026-06-21）**：整頁 9:16，列高用**手調比例** `story.TEMPLATE_HEIGHTS`（非 heat 驅動，避免低 heat 格被壓成 5:1 letterbox）。
  `compose_page_hero(rows, heights=...)`；實測每格 0.68~2.2:1（角色格直/方、場景格橫、Hero 4:3 大塊）。
- ✅ 已做：`choose_template`（回 id）+ `TEMPLATE_HEIGHTS`（手調列高）。⚠️ 待接：`_render_slant` 依 id 組對應 row + 傳 heights（T4 需多生兩張反應圖）。

**一格 meme**：滿版爆笑圖 + 上 setup / 下 punchline。Marvin 看反差（`meme_needs_marvin`）：
- 強反差 → 單飛（不要 Marvin，避免解釋笑話）。
- 反差中 → Marvin 補刀救援。

**已寫好的純函式**：`story.choose_format` / `story.fuse→StoryPlan` / `story.build_title_prompt` /
`highlight.contrast_score` / `highlight.meme_needs_marvin` / `layout.compose_meme`。

**render 端待接（要 API，等額度）**：
1. `render_story(plan)`：StoryPlan → 出圖（Hero duo=setup+reaction 兩張、context=物件、或 meme 單張）+ 清理 punchline + 生標題/馬文 + 標題bar 拼版。
2. 接進 poster（取代純日誌路徑）。
3. populate `panel.inset`（笑點反應特寫，選配）。

## D. 下個月接線清單（額度回來照這個接）

**現況**：找笑點→融合→出圖→拼版**全寫好且測過**（純函式、注入式 img_fn/text_fn）。
跑著的 poster 還是**舊的純日誌路徑**（`render_session`）。差的就是「餵真 LLM + 換成故事路徑」。

**Step 0 — 確認額度**：去 ai.studio/spend 看月支出上限是否重置/已調高；先用 demo 出一張確認付費呼叫不再 429。

**Step 1 — 升級 poster（LIVE `diary_comic_poster.py::_render_blocking`）**：
- 現在：`render_session(session, ...)`（純日誌）。
- 改成：
  1. `from diary_comic.highlight import find_highlights`
  2. 撈該 session 時間範圍的 transcripts（`marvin.db`），`find_highlights(rows)`。
  3. `from diary_comic.story import fuse` → `plan = fuse(diary_session, highlights)`。
  4. `plan is None` → 不出（沒笑點，符合 B）。
  5. `from diary_comic.render import render_story` → `page = render_story(plan, img_fn=_img_fn(key,guard), text_fn=_text_fn(key), cache_dir=CACHE_DIR)`。
  6. cap 估算改成：meme=1 張、slant=context+2(Hero duo)；`guard.allow(0.04*張數)`。
- `_img_fn`/`_text_fn` 沿用現成（已含 PaidUsageGuard 入帳）。

**Step 2 — 視覺驗收**（出一張看，確認都生效）：
- 表情豐富（眉/眼/嘴+汗滴）、鏡頭變化、rule of thirds/景深、變動 gutter、Hero 破格。
- Hero 拆兩拍（上鋪哏 setup 圖、下哄堂笑 reaction 圖）。
- meme 模板有挑對、強反差單飛/反差中馬文救援。
- 標題 bar 正常。

**Step 3 — inset 反應特寫（選配，最後一哩）**：
- `render_story` 的 Hero reaction panel 多生一張「笑臉極特寫」，塞進 `panel.inset`（`Panel.inset` 欄位 + `compose_page_hero` 要加 `_draw_inset_corner` 呼叫；目前只 webtoon 接了）。

**關鍵檔**：`diary_comic/{highlight,story,render,layout,camera,panel_gen}.py` + LIVE `diary_comic_poster.py`。
**測試**：`venv_simon/bin/python -m pytest -k diary_comic`（150 綠）。

## 來源
- Komawari basics — globalcomix.com/news/details/254
- Pro paneling guide — clipstudio.net/how-to-draw/archives/160963
- Frame layout — medibangpaint.com（manga tutorial 06）
- Laying out speech bubbles — manga-with-stef.com/laying-out-speech-bubbles
- Mastering manga panels — animecx.com/manga-panels
- 表情 prompt：ai-prompt.jp facial-expression、Danbooru tags（ipic.ai）、midjourney facial expression（openart.ai）
