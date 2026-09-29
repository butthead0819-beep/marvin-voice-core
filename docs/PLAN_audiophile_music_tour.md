# PLAN: Marvin 聽覺放大鏡與深度音樂導覽（Audiophile Music Tour）

> **給接手 Agent（Claude Code）的執行規範**：
> 1. 請務必遵照本專案 `CLAUDE.md` 的憲法級規則：**先寫測試（TDD）、確認全紅、最小實作、確認全綠、測試與實作同 commit**。
> 2. 所有註解、文件與回應一律使用**繁體中文（台灣口語）**。
> 3. 遵守 `AGENT_MEMORY.md` 既有經驗：不得自開 client、所有 LLM 呼叫走統一路徑、不得寫死模型名稱。

---

## 🎯 核心需求與目標

使用者需要一套**「極具深度、零幻覺」**的音樂導聆系統，而不是淺層的客套接話或維基百科報幕：
1. **聽覺放大鏡（Active Listening Guide）**：
   - 告訴聽眾耳機裡要「仔細聽什麼」（樂器獨特選用、聲場立體定位、和弦離調、突變瞬間、錄音真實呼吸聲）。
2. **Google Search Grounding 查證（零幻覺保障）**：
   - 利用 Gemini 2.0 Flash 原生 `google_search` 工具，在**單一次呼叫（Single-Turn）**內自動搜尋專業樂評、錄音訪談與製作幕後，提煉出真實細節，嚴禁憑空捏造。
3. **時長與字數放寬**：
   - 導聆長度放寬至 **20 秒（約 90~110 個中文字）**，不再受限於舊版 crossfade 的 8 秒/55 字限制，徹底避免殘句截斷。
4. **⚠️ 硬性核心規則：先聽完導覽，音樂才從 00:00 開播（Sequential Pre-roll）**：
   - **絕不**在導覽講話時偷跑音樂前奏！避免 20 秒導覽講完，歌曲精彩的前奏已經播過頭。
   - 流程：上一首結束 ➔ 播放 20 秒導覽音訊（背景可配微弱極簡環境音 Pad）➔ 導覽結束留白 1 秒 ➔ 下一首音樂正式從第 0 秒爆發出來。
5. **架構整合與指令鎖定**：
   - 透過 `IntentContext(mode="album_tour")` 鎖定一般音樂控制指令，避免一般切歌打亂導覽。
   - 具備本地快取機制（`SongKnowledgeStore`），同一首歌二次播放零 API 成本。

---

## 🏗️ 系統架構與模組設計

```
[使用者 /tour 或 /guide_next]
          │
          ▼
┌──────────────────────────────────────────────┐
│  1. AudiophileGuideFetcher (Gemini Grounding) │
│     - 單次 API 呼叫：Google Search + 寫導聆稿   │
│     - 本地 JSON 快取 (records/song_knowledge.json)
└──────────────────────┬───────────────────────┘
                       │ 產出 90~110 字台詞
                       ▼
┌──────────────────────────────────────────────┐
│  2. TTS 預先渲染 (Pre-render TTS)              │
│     - 轉為本機音檔並計算精確秒數 dur_s          │
└──────────────────────┬───────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────┐
│  3. Sequential Playback Pipeline (MusicCog)   │
│     Step A: vc.play_dj_on_tts_layer(audio)   │
│     Step B: await asyncio.sleep(dur_s + 1.0) │ ◄── 嚴格等待導聆結束！
│     Step C: vc.play_stream_song(00:00)       │ ◄── 音樂從第 0 秒開播
└──────────────────────────────────────────────┘
```

---

## 📋 逐步實作計畫（Step-by-Step Implementation）

### Phase 1: 提示詞建造器與規範中心（`dj_prompt_builder.py`）
- [ ] **Step 1.1（TDD）**：在 `tests/test_dj_prompt_builder.py` 新增測試：
  - `test_build_audiophile_guide_prompt()`
  - 驗證 Prompt 包含：90~110 字長度規範、聽覺線索引導（聲場/樂器/突變）、禁止假文青套話、只輸出台詞。
- [ ] **Step 1.2（實作）**：在 `dj_prompt_builder.py` 實作 `build_audiophile_guide_prompt(song_label: str) -> str`：
  - 採用「三幕式聽覺導引結構」：
    1. 破除既定印象（~25 字）
    2. 核心音軌細節與聽覺錨點（~55 字）
    3. 戴耳機進歌引導（~20 字）

### Phase 2: Gemini Google Search Grounding 抓取器（`audiophile_fetcher.py`）
- [ ] **Step 2.1（TDD）**：在 `tests/test_audiophile_fetcher.py` 編寫單元測試：
  - Mock Gemini 的 `aio.models.generate_content` 回應。
  - 測試快取命中機制（`records/song_knowledge.json` 已有紀錄時不打 API）。
  - 測試無快取時帶 `types.Tool(google_search=types.GoogleSearch())` 呼叫。
  - 測試失敗退路（Fallback 保底台詞）。
- [ ] **Step 2.2（實作）**：建立 `audiophile_fetcher.py`：
  - 參考 `intent_agents/grounded_qa_agent.py` 的 Grounding 呼叫寫法。
  - 實作 `fetch_audiophile_guide(title: str, artist: str) -> str`。
  - 整合 `SongKnowledgeStore` 讀寫快取。

### Phase 3: 播放管線改造 —— 嚴格循序播放（`MusicCog`）
- [ ] **Step 3.1（TDD）**：在 `tests/test_audiophile_playback_sequence.py` 編寫播放順序測試：
  - 驗證當 `info['_audiophile_guide']` 存在時：
    - `play_dj_on_tts_layer` 必須先被呼叫。
    - 在 TTS 播放完畢前，`play_stream_song` **絕對不能** 被觸發。
    - TTS 播完後，音樂播放器被呼叫，且起始位置為 0 秒。
- [ ] **Step 3.2（實作）**：在 `cogs/music_cog.py` 的 `_stream_loop` 或 `_stream_loop_prepare_and_announce`：
  - 當檢測到 `info.get('_audiophile_guide')` 時：
    ```python
    # 循序播放守門：先聽完導覽才開始放，避免 20 秒跳過該聽的位置
    guide_audio = info.get('_audiophile_guide_audio')
    guide_dur = info.get('_audiophile_guide_dur', 0.0)
    if guide_audio and guide_dur > 0 and vc is not None:
        with vc._protected_tts_window():
            await vc.play_dj_on_tts_layer(guide_audio, text=info.get('_audiophile_guide_text'))
            await asyncio.sleep(guide_dur + 1.0)  # 播完留白 1 秒
        # 標記本首不需要再在尾段或開頭重複疊播 DJ 口白
        info['_dj_played_in_tail'] = True
    ```

### Phase 4: IntentBus 模式鎖定與 Slash 指令
- [ ] **Step 4.1（實作）**：新增 Slash 指令 `/guide_song`（對下一首或當前點播歌掛載深度導聽）：
  - 在 `cogs/music_cog_commands.py` 註冊：
    `/guide_song <song_name>`：搜尋並加入佇列，打上 `_audiophile_guide=True` 標籤，背景提前跑 Grounding + TTS 渲染。
- [ ] **Step 4.2（實作）**：新增專輯巡禮指令 `/tour <artist> <album>`：
  - JIT 生產線：解析專輯曲目，排入 `stream_queue`，標記 `_lane='album_tour'`。
  - 設定 `current_ctx.mode = "album_tour"`，IntentBus 自動屏蔽一般切歌/點播。

### Phase 5: 驗證腳本與真機測試
- [ ] **Step 5.1**：提供離線預覽腳本 `scripts/preview_audiophile_guide.py`：
  - 執行 `python scripts/preview_audiophile_guide.py --artist 周杰倫 --song 雙截棍`。
  - 印出：Google 搜尋到的關鍵來源、生成出的 20 秒導聆台詞、TTS 合成後的 wav 檔案路徑與精確秒數。
- [ ] **Step 5.2**：執行全套 pytest 迴歸測試：
  ```bash
  source venv_simon/bin/activate
  python3 -m pytest tests/test_dj_*.py tests/test_audiophile_*.py -q
  ```

---

## 🔒 關鍵護欄（Gotchas & Guardrails）

1. **防截斷保護**：
   - 導聆口白務必包裹在 `with vc._protected_tts_window():` 內，禁止被任何 barge-in、打斷機制或短句長度裁切截斷。
2. **Token 與搜尋成本控制**：
   - 相同的 `(artist, title)` 組合在成功生成一次後，必須永久存入 `records/song_knowledge.json` 的 `audiophile_guide` 欄位。第二次點播同一首歌時直接讀取，**零 API 成本**。
3. **TTS 語音品質**：
   - 導聆語音建議指定沉穩、知性、具有音樂質感的 Voice（例如 macOS 本機高品質語音或專屬 TTS 預設），不與平常的厭世諷刺短語混淆。

---

# v2 重新設計（2026-09-29 使用者定案）

## 為什麼改
1. Marvin DJ 主體是 autopilot——導聆要長在 DJ 串場裡，不是只靠 `/guide_song` 手動觸發。
2. `/tour 歌手 專輯`：使用者不知道專輯名、EP 太短，很快沒人用 → 改成**從曲庫已累積的導聆拼接**。
3. 曲庫歌手/專輯要正規化——用同一次 grounded 呼叫順便產出，只用免費層、靠日常點歌/串場慢慢累積。
   （現況佐證：舊 `get_or_extract_insight` 的維基賞析會把〈葉子〉講成植物、〈新造的人〉講成聖經條目，已違反「說錯不如沒說」。）

## 定案
- **歌曲卡（song card）**：一次 grounded 呼叫輸出固定行格式——歌手／歌名／專輯（單曲寫「單曲」）／年份／導聆（90-110 字長版）。
  快取：`canon::<videoId>` → 正規化欄位；`audiophile::<正規化歌手> - <正規化歌名>` → 長版導聆（沿用舊 key 格式）。
- **自動觸發只走免費層**（paid_client=None）：DJ 串場 prefetch 時查快取，未命中且預算允許就背景查；
  最多等 10s，沒回來這輪就不用、task 繼續跑完寫快取給下次。預算：全域間隔 ≥90s、每日上限 100 次、同一首失敗 7 天內不重試。
- **短版＝DJ 串場既有那次 LLM 呼叫改寫**（零額外呼叫）：`select_mode` fallback 輪替多一個 `guide`（有歌曲卡才候選、排第一），
  選中時把長版導聆放進 ctx 當唯一事實素材。頻率交給既有輪替，不另外寫死。
- 舊維基「音樂賞析」ctx 移除，改用歌曲卡的「歌手《專輯》年份」行。
- `/guide_song` 維持長版 pre-roll，但改走歌曲卡（順便正規化；手動指令可用付費鏈，照舊記帳）。
- **`/tour 歌手`**（第二刀）：從 `canon::` 找該歌手、且有導聆的歌，依年份/專輯排序；直接播 videoId（不再 YouTube 搜尋配錯歌）；
  每首長版 pre-roll、零新 grounded 呼叫。拔掉 `fetch_album_tracklist` 與 album 參數。少於門檻首數就回報「曲庫還不夠」。

## 刀序
1. 歌曲卡 + 免費預算 + DJ guide 模式 + `/guide_song` 改接（本刀）
2. `/tour 歌手` 曲庫拼接

### 第一刀實測修正（9/29 已 land f68773a）
- 「一次 grounded 拿正規化+導聆」作廢：五行格式 prompt 讓 Gemini 不搜尋（0/3 被 L2 擋），舊導聆 prompt 3/3。
- 免費 gemini-2.5-flash 一天只有 **20 次**（GenerateRequestsPerDayPerProjectPerModel-FreeTier，跟 AmbientQA 共用）；
  2.5-flash-lite 有額度但不搜尋；3.x flash-lite 一加 google_search 就 429。
- 正規化改 **iTunes Search（country=TW）**：免費無額度、結構化。守門=曲名拼音互含 + 歌手（合唱拆開）出現在原始標題/頻道名。
  抽樣 25 首命中 10、加歌手守門後 0 配錯（誤擋羅馬拼音↔中文名如 A-Sun↔阿桑，接受）。命中率受 song_name_clean 髒標題限制。
- 預算：autopilot/個人歌單只免費、每日 10 次；真人點歌免費→付費。
- **導聆 Prompt 去模板化與雙軌縫合（9/29 改進）**：
  1. **Prompt 去模板化**：`dj_prompt_builder.py` 封殺「許多人以為」、「別以為」、「這不只是」、「其實它更」等八股反轉句型；引入四大開場切入角度（製作幕後軼事、歌手唱腔紋理、聽覺焦點、創作心境），實測周杰倫〈雙截棍〉直接以「最初寫給張惠妹遭退稿」幕後破題，文案達到專業音樂電台水準。
  2. **雙軌縫合（Bimodal Stitching）**：解決「生活話題與歌曲導聆二元割裂」硬傷。在 `music_cog_dj_lyrics.py` 中，當選中生活話題/頻道對話（`life` / `conversation` / `memory_match`）且歌曲有快取的 `guide` 導聆時，自動將聽覺彩蛋作為接歌橋樑注入 context，讓 Marvin 在 45-55 字內同時呼應聽眾近況並引出歌曲聽覺焦點。

---

# v3 多維度扭蛋素材卡片 ＋ 零塑膠感串場 ＋ 鋼鐵防線系統（2026-09-29 定案）

## 一、核心架構理念

1. **多維度歌曲卡（The Multi-Faceted Song Card）**：
   - 擺脫單一「樂器/聲場」的生硬感，將歌曲資料擴充為四大面向：
     - **【維度 A：正規化曲目】**：歌手、歌名、專輯、年份（iTunes API，免費，0 次 LLM）。
     - **【維度 B：完整歌詞與時間戳】**：精確至 `[mm:ss]` 的歌詞（syncedlyrics，免費，0 次 LLM）。
     - **【維度 C：聽覺與錄音室人間劇（Studio Lore）】**：錄音花絮、爭吵、退稿軼事、真實呼吸聲（Google Grounding）。
     - **【維度 D：社群記憶與時代眼淚（Social Lore）】**：PTT/YouTube 熱評標籤、失戀破防神曲、KTV 大合唱迷因（Google Grounding）。
     - **【維度 E：歌詞靈魂刺點（Lyric Punchline & Subtext）】**：直擊痛點的單句歌詞、時間戳、解碼表面說辭背後的殘酷潛台詞。

2. **扭蛋式素材 ✕ 4 種真實老友切入動機（徹底消滅 AI 塑膠感）**：
   - 嚴禁使用「文藝、溫柔、電影感」等讓 LLM 假裝感性的塑膠濾鏡。
   - 永遠錨定在 **Marvin「住在伺服器裡的毒舌/默契機器人老朋友」** 單一人設，每次隨機抽一個世俗的「說話動機」切入：
     - **動機 1【抓矛盾吐槽 (Spot the Irony)】**：藉由歌詞刺點戳破人類感情或生活的小矛盾。
     - **動機 2【爆世俗小八卦 (Spill the Tea)】**：分享錄音室被退稿、出錯的真實八卦。
     - **動機 3【丟個聽覺懸念 (Drop the Hook)】**：點出某個奇怪的樂器或 1 分多鐘突然沉下去的低音。
     - **動機 4【給現在氣氛點題 (Name the Vibe)】**：觀察頻道聽眾現在有多累多廢，把音樂當作解藥。

---

## 二、極致成本控制：單曲「1 次 API 呼叫」聚合查證

為了保護免費層（每日 20 次）並壓低付費層成本，**嚴禁將素材分開呼叫多次**。

```
[歌曲初次建檔流程]
  1. iTunes API ──▶ 正規化歌手/歌名/專輯/年份 (0 次 LLM, $0)
  2. syncedlyrics ──▶ 抓取完整歌詞與 [mm:ss] 時間戳 (0 次 LLM, $0)
  3. Gemini 2.5 Flash (帶 google_search) ──▶ 【單次聚合查詢】 (1 次 LLM, ~$0.035 美元)
       輸入：歌名 + 歌手 + 精選歌詞片段
       一次輸出三行：
         【聽覺與幕後】...
         【社群熱評標籤】...
         【歌詞刺點與潛台詞】...
  4. 寫入 records/song_knowledge.json ──▶ 永久快取！
```

- **重播（Cache Hit）**：**0 次呼叫、0 元**。
- **費用評估**：
  - 免費層：每日 15~20 首新歌完全免費（每月可累積 450+ 首歌）。
  - 付費層：每首全新歌建檔約 **台幣 1.1 元**（建檔後永久免費）。

---

## 三、品管鋼鐵防線：怎麼防？防什麼？翻車怎麼辦？

### 1. 防什麼？
- **字數超時（爆字數）**：超過 56 字會被前奏截斷產生殘句。
- **AI 塑膠腔與假文青**：「身為AI」、「撫平心靈」、「時光流淌」等空話。
- **幻覺與越界掛名**：把沒點播的人說成點播、把聽眾經歷說成自己的。
- **殘句與語句破碎**。

### 2. 翻車之後怎麼來得及出聲？（零延遲階梯降級機制）
> ⚠️ **硬性鐵則：一旦 LLM 輸出被擋下，絕對不准「重新呼叫一次 LLM」（重打需 2~3 秒，音樂早播過了，絕對來不及）！**

```
┌─────────────────────────────────────────────────────────────┐
│ 1. 事後微整形修剪 (Micro-Trimming，耗時 < 1ms)                │
│    - 若字數微超標（如 58 字）：                               │
│      先剝除「上一首剛播完」子句，或截至最後一個句號，保留完整語意。│
│    - 檢查合格 ──▶ 直接送 TTS 渲染出聲！                      │
└──────────────────────────────┬──────────────────────────────┘
                               │ 【嚴重違規/踩禁詞/幻覺/空字串】
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. 本地零延遲兜底扭蛋 (Zero-Latency Fallback Bank，耗時 0.1ms) │
│    - 嚴禁重打 LLM！                                         │
│    - 從本地 dj_comedy_fallback.py 抽一則生活微幽默：            │
│      「據觀察，世界上最遠的距離是躺平後發現電燈沒關。聽首歌吧。」   │
│    - 固定 42 字、零幻覺、零 AI 腔 ──▶ 立刻送 TTS 出聲！        │
└──────────────────────────────┬──────────────────────────────┘
                               │ 【極端異常】
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. 硬體級永不翻車保底 (Hardcoded Segue，耗時 0.01ms)           │
│    - 「DJ Marvin 為你帶來《{歌名}》，希望大家喜歡。」          │
└─────────────────────────────────────────────────────────────┘
```

---

## 四、分刀落地實施路線（Roadmap）

- [x] **Slice 1（9/29 已完成）**：
  - Prompt 破除模板化（封殺八股反轉、引入多維度開門見山）。
  - 短版 DJ 串場雙軌縫合（生活話題 ＋ 導聆接歌橋樑）。
- [ ] **Slice 2（單次聚合卡片 Ingestion 引擎）— 半成品，未接線**：
  - ✅ `build_song_card_ingestion_prompt` / `parse_song_card_response` / `fetch_song_card` 已寫、有單元測試。
  - ⬜ prod 沒有任何呼叫端；`social_lore`/`lyric_hook` 在 prod 永遠空。
  - ⚠️ 接線前要先實測：多段格式 prompt 9/29 已實測讓 Gemini 不搜尋（0/3 被 L2 擋），三段式很可能重蹈覆轍。
  - ⚠️ 舊快取同 key 已有 `audiophile_guide` → 會直接命中，永遠不升級成三段卡。
  - ⬜ 維度 B（syncedlyrics 歌詞時間戳）未做，`lyrics` 參數未傳入。
  - 🧪 9/30 免費層實測 5 首：4 首 429（額度跟 AmbientQA 共用），僅〈葉子〉成功——**有搜尋**（4 來源），但導聆 155 字超標（規格 90-110）。
    同一筆回應驗出 parser 兩 bug 已修：欄位分行寫整段歌詞刺點被丟、兩組標籤時標籤/情境配錯對。
    時間戳改不向 LLM 要（沒餵同步歌詞時「約 02:00」是猜的）；要時間戳等接線時本地用 syncedlyrics 對。
  - ⬜ 額度重置後再測 5 首（間隔 60s）確認搜尋率與長度，才決定接線。
- [x] **Slice 3（扭蛋式話題組裝器 Gacha Narrator）**：
  - `dj_gacha_narrator.py` 已接入 `_fetch_dj_interjection_raw` guide 模式。
  - 現況只抽得到 hook/tea（irony/vibe 要等 Slice 2 接線才有素材）。
- [ ] ~~即時品管回饋監測 `dj_feedback_watcher.py`~~ — **9/29 撤回**：沒接切歌、關鍵字對全頻道閒聊誤判（任何人說「好吵」就靜音 5 分鐘）、每句命中都 DM、hook 掛在共用骨幹 ConversationBuffer。要做先決定該不該存在。
- [ ] **Slice 4（品管與本地救火）— 部分**：
  - ✅ `_is_qualified_dj_script` 改用 `FORBIDDEN_DJ_PHRASES` 單一來源；`COMEDY_FALLBACK_SCRIPTS` 清成零禁詞。
  - ⬜ 笑話兜底池**未接進** LLM 不合格降級鏈（現況仍是 autopilot 模板 → 硬報幕）。
  - ~~PROTEST 冷卻靜音~~ 隨 feedback_watcher 撤回。
