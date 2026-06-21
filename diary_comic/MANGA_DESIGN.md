# 漫畫產生器設計參考（日式分鏡 + 表情 prompt）

2026-06-21 上網研究後整理。對照 `diary_comic/` 現況，標出已做 ✅ / 待改 ⚠️ / 新點子 💡。
來源見文末。

---

## A. 日式分鏡核心原則

### 1. 切格大小 = 強弱與節奏（最重要）
- 大格放高潮 / 角色登場 / establishing；小格放鋪陳與快節奏。
- **Splash panel**（一格佔整頁）= 關鍵爆點。
- 現代漫畫（火影/海賊）約 3 格/頁，emphasis 驅動，不是塞滿。
- ✅ 已做：格子大小 = `heat`（`slanted_bands` / `plan_boxes`）。日漫 4 格、條漫 6 格。Hero ≈ splash。

### 2. Gutter（格間距）編碼「時間」
- 橫向 gutter > 縱向 gutter。
- **窄 gutter = 同時 / 連續 / 快**；**寬 gutter = 時間流逝 / 留白思考**。
- ⚠️ 待改 #1：目前 gutter 均勻。可改成「同場景 beat 之間窄、跳話題之間寬」。

### 3. 斜格 / 傾斜分鏡 = 張力 / 動作 / 速度（選擇性，非每格）
- 對角切 = drama；圓弧格 = 平靜。
- **Broken border**（角色破格衝出邊框）= 強烈動感。
- ✅ 已做：只有 Hero 斜切（`hero_split_polys` / `compose_page_hero`）。
- ⚠️ 待改 #2：Hero 角色可破格衝出。

### 4. 鏡頭變化
- 正面/斜/側/背 × 特寫/遠景/俯視/仰視；rule of thirds（主體偏離中心）。
- 極特寫 = 親密；遠景 = 交代場景。
- ✅ 已做：`camera.py::shot_for`（第一格 establishing、Hero 低角度仰拍、中段輪替）。
- ⚠️ 待改 #4：擴鏡頭詞彙（極特寫反應、silhouette、broken-frame）。

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

## 來源
- Komawari basics — globalcomix.com/news/details/254
- Pro paneling guide — clipstudio.net/how-to-draw/archives/160963
- Frame layout — medibangpaint.com（manga tutorial 06）
- Laying out speech bubbles — manga-with-stef.com/laying-out-speech-bubbles
- Mastering manga panels — animecx.com/manga-panels
- 表情 prompt：ai-prompt.jp facial-expression、Danbooru tags（ipic.ai）、midjourney facial expression（openart.ai）
