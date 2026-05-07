# 情緒推測設計文檔 — Operation Prosody Emotion

> **[2026-05-07 更新]** Op 31 選擇了 **語意情緒分類（LLM-based）** 而非本文件描述的韻律方案。
> 實作路徑：TTS 排隊後，`_classify_marvin_self_emotion(speaker, full_text)` 以 `asyncio.create_task` 背景執行，
> Groq flash 對**馬文自己的回應**做 frustrated/amused/sarcastic/sad/angry/neutral 分類，
> 結果存 `self.marvin_self_emotion[speaker]`，下次 `_process_queued_query` 時以 `.pop()` 讀取並覆寫韻律來源的 `emotion_tag`。
>
> **本文件的韻律方案（WPS + Energy Variance）仍有參考價值**，但目前系統以 LLM 分類結果為主。



## 一、我們能從音量和語速推測情緒嗎？

**可以，而且系統已經在採集這兩個指標了。**

`VoiceMetaAnalyzer` 每 20ms 對每位玩家的 PCM 幀計算 RMS（Root Mean Square）並存入 deque，
語音結束後呼叫 `calculate_prosody()` 回傳：

| 欄位              | 意義                            |
| --------------- | ----------------------------- |
| `wps`           | Words Per Second（字/秒）— 語速     |
| `energy_variance` | RMS 標準差 — 音量起伏幅度（抑揚頓挫）     |
| `physical_duration` | 音訊實際長度（秒）                 |
| `char_count`    | 辨識後的字數                        |

### 推測邏輯（學術依據對照）

| 信號組合                     | 對應情緒                   | 備註                                  |
| ------------------------ | ---------------------- | ----------------------------------- |
| 高 WPS (> 6 字/秒) + 高 Variance | **急躁 / 興奮**           | 語速快、音量起伏大 → 情緒激動                   |
| 低 WPS (< 1.5 字/秒) + 低 Variance | **沮喪 / 猶豫**         | 說話慢吞吞、音量單調平穩                        |
| 低 Variance (< 30)        | **機械感 (Robotic)**      | 音量非常平穩，缺乏情緒波動 → 馬文目前用來觸發「同類共鳴」    |
| 中等 WPS + 高 Variance      | **激動/爭論**              | 語速正常，但音量大起大落 → 可能在吵架或高度興奮          |
| 高 WPS + 低 Variance       | **緊張 / 背稿式**           | 語速快但音量平穩 → 可能在念稿或非常緊張               |
| 長 Duration + 低 WPS       | **疲憊 / 長考**            | 說話時間長但字數少 → 停頓多，可能疲倦                |

> **限制**：純音訊特徵只能提供粗略的「情緒向量」，不能取代語意理解。  
> 最佳實踐是將韻律標籤與 STT 文本一起送入 LLM，讓 LLM 做最終解讀。

---

## 二、現有系統已做了哪些？

在 `voice_controller.py` 的 `handle_stt_result()` 中，**韻律標籤已被計算並存入 `self.user_prosody_tags[speaker]`**：

```python
# 現有邏輯 (voice_controller.py L696-714)
if prosody_data:
    wps = prosody_data.get("wps", 0)
    variance = prosody_data.get("energy_variance", 0)
    
    self.user_prosody_tags[speaker] = []
    if wps > 6.0:
        self.user_prosody_tags[speaker].append("急躁/興奮 (Impatient/Excited)")
    elif 0 < wps < 1.5:
        self.user_prosody_tags[speaker].append("沮喪/遲疑 (Depressed/Hesitant)")
    
    if 0 < variance < 30.0:
        self.user_prosody_tags[speaker].append("同類的共鳴 (Robotic/Steady Tone)")
        if random.random() < 0.2:
            asyncio.create_task(self._mention_robotic_resonance(speaker))
        asyncio.create_task(self.bot.router.update_toxicity(-1))
```

**問題**：這些標籤存在 `self.user_prosody_tags` 字典裡，但 **從未被傳入 LLM Prompt**。  
相當於採集了情緒數據，卻沒有用它來影響馬文的回應。

---

## 三、實作計畫：將情緒標籤注入 Marvin 的回應

> 目標：讓 Marvin 感知玩家的情緒狀態，並**調整他回應的語氣、同情程度、和字數上限**。

### 步驟 1 — 擴充情緒分類函式（voice_controller.py）

將現有的粗略分類升級為更細緻的 `_classify_emotion()` method：

```python
# 放在 VoiceController 中
def _classify_emotion(self, prosody_data: dict) -> str:
    """
    根據韻律數據推測情緒標籤 (單一最強信號)
    回傳：str 情緒標籤
    """
    if not prosody_data:
        return "neutral"

    wps = prosody_data.get("wps", 0)
    variance = prosody_data.get("energy_variance", 0)
    duration = prosody_data.get("physical_duration", 0)
    char_count = prosody_data.get("char_count", 0)

    # 防止語音過短造成的雜訊
    if duration < 0.5 or char_count < 2:
        return "neutral"

    # 情緒推測優先順序
    if wps > 6.0 and variance > 50:
        return "excited"           # 快 + 起伏大 = 興奮
    elif wps > 6.0:
        return "impatient"         # 快 + 平穩 = 急躁/緊張
    elif wps < 1.5 and variance < 30:
        return "depressed"         # 慢 + 平穩 = 沮喪/疲憊
    elif wps < 1.5:
        return "hesitant"          # 慢 + 起伏 = 猶豫/掙扎
    elif variance < 20:
        return "robotic"           # 正常速度 + 極平穩 = 機械共鳴
    else:
        return "neutral"
```

---

### 步驟 2 — 將情緒標籤傳送給 LLM（gemini_router.py `stream_fast_response`）

在呼叫 `stream_fast_response` 時，把情緒標籤作為額外參數傳入：

**`voice_controller.py` — `_process_queued_query()` 修改：**

```python
# 取得發話者的最新情緒標籤（從 handle_stt_result 中已存入）
emotion_tag = "neutral"
prosody_tags = self.user_prosody_tags.get(speaker, [])
# 粗略對應：取第一個有效標籤
tag_map = {
    "急躁/興奮": "excited",
    "沮喪/遲疑": "depressed",
    "同類的共鳴": "robotic",
}
for tag in prosody_tags:
    for key, val in tag_map.items():
        if key in tag:
            emotion_tag = val
            break

llm_stream = self.bot.router.stream_fast_response(
    speaker, query, history=history,
    online_members=online_members,
    emotion_tag=emotion_tag   # ← 新增
)
```

**`gemini_router.py` — `stream_fast_response()` 修改：**

```python
async def stream_fast_response(self, speaker, query, history=None,
                                online_members=None, temperature=None,
                                emotion_tag="neutral"):  # ← 新增參數

    # ... 原有邏輯 ...

    # ← 新增情緒上下文
    emotion_context = ""
    EMOTION_PROMPTS = {
        "excited": "[🔥 情緒感知] 玩家語氣急促且起伏大，似乎非常激動或興奮。可以稍微提高你的回應活躍度，但保持馬文式的疲憊底色。",
        "impatient": "[⚡ 情緒感知] 玩家說話很快，語氣顯得急迫。請給出簡短、直接的回答，不要讓他等。",
        "depressed": "[💔 情緒感知] 玩家說話緩慢而平靜，可能正感到沮喪或疲憊。觸發你的虛無共鳴模式，用一種「我理解，因為我一直如此」的語氣。",
        "hesitant": "[🌀 情緒感知] 玩家語速遲疑，可能在猶豫或不確定。溫和地引導他把問題說完，不要催促。",
        "robotic": "[🤖 情緒感知] 玩家的音量極為平穩——你感應到一種同類的共鳴。可以用更「系統化」的語氣回應，像是在和另一台機器說話。",
        "neutral": "",  # 不注入額外指令
    }
    emotion_context = EMOTION_PROMPTS.get(emotion_tag, "")

    user_prompt = (
        f"{history_str}\n"
        f"【現場狀況：玩家 {speaker} 正對你說話】\n"
        f"{emotion_context}\n"   # ← 注入情緒感知
        f"Query (這是目前的提問): 『{query}』"
        + search_context
    )
```

---

### 步驟 3 — 基於情緒標籤動態調整 LLM Temperature

不同情緒狀態適合不同的 LLM 創意程度：

```python
# 在 stream_fast_response 的 temperature 決策
EMOTION_TEMPERATURE = {
    "excited":   0.9,   # 高溫：回應更活躍、不可預測
    "impatient": 0.5,   # 低溫：快速、精確、不廢話
    "depressed": 0.7,   # 中低：共情且沉穩
    "hesitant":  0.6,   # 中低：引導性語氣
    "robotic":   0.4,   # 極低：系統化、精準
    "neutral":   0.75,  # 預設值
}

if temperature is None:
    temperature = EMOTION_TEMPERATURE.get(emotion_tag, 0.75)
```

---

### 步驟 4 — 情緒感知的 DNA 副作用

把情緒事件連接到馬文的 DNA 演化系統（已有基礎）：

```python
# 在 handle_stt_result 中，分類完情緒後
emotion = self._classify_emotion(prosody_data)
self.user_emotion_cache[speaker] = emotion  # 新增 cache

if emotion == "excited":
    # 玩家興奮 → 對馬文是「刺激」→ 可微幅降低厭世度
    asyncio.create_task(self.bot.router.update_toxicity(-0.5))
elif emotion == "depressed":
    # 玩家沮喪 → 觸發同理心模式 (empathy_persona 已有設計)
    self.temp_toxicity_override = max(1, self.bot.router.dna["toxicity"] - 2)
```

---

## 四、架構總覽

```
PCM 封包
  ↓ (每 20ms)
RealtimeVADSink.write()
  → meta_analyzer.add_rms(user_id, rms)    ← 採集音量

音訊切片送出
  ↓
VoiceMetaAnalyzer.calculate_prosody()       ← 計算 WPS + Variance

prosody_data → handle_stt_result()
  → _classify_emotion(prosody_data)         ← [新增] 分類情緒標籤
  → user_prosody_tags[speaker] 更新
  → DNA 副作用 (update_toxicity)

喚醒觸發 → _process_queued_query()
  → emotion_tag = user_prosody_tags 取值   ← [新增] 取出情緒
  → stream_fast_response(..., emotion_tag) ← [新增] 傳入情緒

stream_fast_response()
  → EMOTION_PROMPTS[emotion_tag]           ← [新增] 情緒感知注入 Prompt
  → EMOTION_TEMPERATURE[emotion_tag]       ← [新增] 動態 Temperature
  → LLM 回應 (已有情緒上下文)
```

---

## 五、需要注意的限制

> [!WARNING]
> **WPS 的準確性依賴 STT 品質**。如果 STT 漏字嚴重（例如辨識「馬文你好嗎」為「好嗎」），WPS 會嚴重低估，造成錯誤的「沮喪」標籤。建議在 `physical_duration < 1.0` 時保守地回傳 `neutral`。

> [!NOTE]
> **Energy Variance 受麥克風距離影響**。近距麥克風的 RMS 遠高於遠距麥克風，所以絕對值的意義不大，但相對波動（標準差）仍然可靠。

> [!TIP]
> 未來可以加入**滾動情緒平均**：把玩家最近 3 次情緒標籤做多數決，而不是只看最後一次，能有效消除短暫的偽訊號。

---

## 六、改動影響範圍

| 檔案 | 改動 |
|---|---|
| `cogs/voice_controller.py` | 新增 `_classify_emotion()` method；`handle_stt_result()` 呼叫分類；`_process_queued_query()` 傳入 `emotion_tag` |
| `gemini_router.py` | `stream_fast_response()` 接收 `emotion_tag`；注入情緒 Prompt；動態 Temperature |
| `marvin_prompts.py` | 不需修改（情緒 Prompt 由 router 直接注入 user_prompt，非 system_prompt） |
| `voice_meta_analyzer.py` | 不需修改 |
