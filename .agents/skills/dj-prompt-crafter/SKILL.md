---
name: dj-prompt-crafter
description: Marvin Discord Voice Bot 的 DJ Prompt 撰寫、調優與品質守門指南。用於設計或修改 DJ 串場、報幕、故事弧的 Prompt 與測試。
---

# DJ Prompt Crafter — Marvin Voice Bot

本 Skill 提供 Marvin Discord Voice Bot 在音樂串場（Crossfade Interjection）、即時報幕（Now Playing）、故事弧（Story Arc）與主題歌單等所有 DJ 場景的 Prompt 撰寫、規則維護與測試 SOP。

---

## 🏛️ 五大不可動搖核心原則 (Unified Core Pillars)

所有 DJ Prompt 必須遵循以下 5 大原則（由 `dj_prompt_builder.py` 統一守門）：

1. **字數與時間硬上限 (Strict Budget)**
   - **Crossfade 串場 (`dj_interjection`)**：**45~55 中文字**（語音唸完約 8~9 秒）。超過 56 字視為失敗，會導致 Crossfade 截斷。
   - **即時報幕 (`radio_now_playing` / `stream_now_playing`)**：**20~23 中文字**（語音唸完約 6~7 秒）。
2. **機器人觀察者視角 (Robot POV Guard)**
   - Marvin 是**機器人 DJ**，不是人類。
   - 聽眾的生活、興趣、八卦是「他們的」，絕不用第一人稱把聽眾經歷說成自己的（嚴禁「我最近也在學佛」、「我也搬過家」、「我懂那種感覺」）。
   - 可以說「我」，但僅限於「自己這台機器做的事」（如：挑了這首歌、掃描了聊天紀錄）。
3. **防幻覺與素材單一延伸 (Material Guard)**
   - **只用脈絡給的那一項素材寫**，不要自己腦補新話題，不要把多項線索混在一起講。
   - 歌名最多提一次，借題發揮引起對下一首歌的期待感。
4. **掛名嚴格依據脈絡 (Naming Guard)**
   - 只有脈絡明確標註「點播者」或「理由」裡出現的人才能提名字。
   - 絕不自己猜測或指定這首歌是誰點的。
5. **風格調性：Threads 生活廢文小幽默與陪伴感**
   - **接地氣現代社群風格**：以日常微幽默、生活廢文共鳴（加班、早起、拖延症、購物車、手搖飲、放空）為主。
   - **嚴禁大道理心靈雞湯與說教**：不裝深沉、不搞人生哲理。
   - **嚴禁假文青套話與禁詞**（見下表）。

---

## 🚫 絕對禁止詞與套話清單 (Forbidden Phrases)

LLM 產出若包含以下任何詞彙，本地品管層將直接判為不及格並觸發 Fallback：

| 禁詞類別 | 禁止詞彙 / 套話 | 為什麼禁止 |
|---|---|---|
| **假文青 / 空泛套話** | `時光流動`、`歲月靜好`、`撫平心靈`、`流淌的旋律`、`人生就像一場旅行` | 空洞無物、無病呻吟 |
| **生硬 AI 腔** | `身為AI`、`身為一個AI`、`作為一個人工智慧` | 破壞沉浸感 |
| **陳腔濫調開場** | `大家好我是`、`這首歌送給`、`這簡直就在說你`、`這根本在說你` | 公式化、機械感嚴重 |
| **生硬計數** | `第 X 次點這首`、`共 X 次` | 改用「XXX 常聽這首，在場的 YYY 也是」 |

---

## 🎯 多維度破題技法 (Segue & Hook Strategies)

撰寫 Prompt 時應引導 LLM 在以下維度中自然取材破題：

1. **諧音雙關 (Pun & Wordplay)**：歌名或歌手名諧音接歌（例如：《安靜》➔《開門大吉》）。
2. **近期對話 (Chat Hook)**：抓取頻道近 5 分鐘聊天關鍵字或發言吐槽點。
3. **新聞時事 (News Hook)**：簡短提及輕鬆生活/科技新聞（已自動過濾負面社會與政治案件，2 小時不重複）。
4. **自然社交偏好 (Social Bridge)**：「這首 {A} 很常聽，在場的 {B} 也常點過」。
5. **使用者生活事件 (User Callback)**：以老朋友記得你講過的話那種默契感帶入（需在場人守門）。
6. **音樂幕後典故 (Music Trivia)**：作詞作曲背景、同專輯小故事。

---

## 🛠️ 開發與修改 SOP

### 1. 修改 Prompt 規則
- **單一真實來源**：編輯 `personas/dj_styles.yaml` 與 `dj_prompt_builder.py`。
- 不要直接在業務邏輯（如 `music_cog.py` 或 `gemini_router_content.py`）中 hardcode prompt。

### 2. 本地驗證測試 (TDD)
每次修改 DJ Prompt 或相關規則後，必須執行以下測試：
```bash
source venv_simon/bin/activate
# 1. 驗證 Prompt Builder 核心護欄
python3 -m pytest tests/test_dj_prompt_builder.py -v
# 2. 驗證全套 DJ 串場迴歸測試
python3 -m pytest tests/test_dj_*.py tests/test_news_fetch.py -q
```

### 3. 品管守門檢查清單
- [ ] 輸出字數是否穩定在 45~55 字？
- [ ] 是否完全無禁詞與假文青修辭？
- [ ] 遇到 LLM 超時/失敗時，是否有 `dj_comedy_fallback.py` 安全接軌？
