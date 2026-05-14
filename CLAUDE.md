
## 語言規則

**所有回覆必須使用繁體中文。** 無論問題是什麼語言，一律以繁體中文回答。

---

## TDD 開發模式（預設行為）

實作任何新功能或修 bug 時，**永遠先寫測試，再寫實作**。不需要用戶提醒。

### 流程

1. **寫失敗測試**：用 `tests/test_<feature>.py` 描述預期行為（assert 什麼、回傳什麼、狀態怎麼變）
2. **確認全紅**：執行 `pytest tests/test_<feature>.py`，確認所有測試都失敗（這證明測試有意義）
3. **寫最小實作**：只寫讓測試通過所需的程式碼，不多也不少
4. **確認全綠**：執行 pytest，全部通過才算完成
5. **Commit**：測試與實作放同一個 commit

### 測試命名原則

- `test_<行為描述>_<預期結果>`，例如 `test_select_theme_rejects_unknown_theme`
- 每個測試只驗證一件事
- Fallback / edge case 一定要有對應測試

### 這個專案的測試慣例

- 使用 `pytest` + `pytest-asyncio`
- Discord 相關（bot、cog）用 `MagicMock` + `AsyncMock`，`bot.cogs.get.return_value = None` 關掉 VoiceController
- DB 操作用 `db_path=":memory:"`
- 不測 Discord embed 格式，只測狀態機行為與回傳值

---

## Voice Agent 設計理念

### 核心原則

**零鍵盤操作**：所有互動必須能透過純語音完成，不得要求使用者切換畫面或輸入文字。

**流水線分層**：每層只做一件事，上下游靠 Protocol 介面解耦：
```
Discord Audio Sink → VAD → STT → pre_filter → Intent Route → LLM / NemoClaw
```

**優雅降級**：每一個 I/O 呼叫都必須有 fallback，不能因單一服務失敗而中斷整條流水線。

---

### STT 層設計規範

#### Protocol 合規
- 所有 STT 實作必須滿足 `protocols.py` 的 `STTService` Protocol
- `transcribe()` 必須回傳 `tuple[str, str]`：`(transcribed_text, engine_name)`
- engine_name 必須是可識別的字串（e.g. `"Swift"`, `"Whisper"`, `"Groq"`），不得空白

#### Async 安全
- **CPU-bound Whisper 必須在 `asyncio.to_thread` 內執行**，包含 segment 迭代——不得在外面迭代 lazy generator（會阻塞 event loop）
- subprocess 呼叫必須用 `asyncio.create_subprocess_exec`，不得用 `subprocess.run`
- Sink 的 `write()` 是同步方法（音訊接收執行緒），非同步任務用 `loop.create_task()`，不用 `asyncio.create_task()`

#### 幻覺過濾
- 向引擎注入的 context strings（如 `STT_CONTEXT_STRINGS`）同時也是幻覺來源；必須呼叫 `is_whisper_hallucination(text, prompt)` 過濾 echo-back
- 幻覺過濾後若文字為空，視為無效轉錄，跳過後續處理

#### 暫存檔清理
- WAV 暫存檔必須在 `finally` 區塊刪除，不得依賴正常流程路徑

#### Lock 範圍
- `stt_lock`（`Semaphore(1)`）只包住 STT subprocess 呼叫，不包住下游 LLM 或 TTS 呼叫
- Lock 範圍過大會導致多人說話時排隊卡死

---

### VAD 層設計規範

**自適應噪音地板（Adaptive Noise Floor）**：
- 使用滾動視窗（75 packets）計算平均 RMS，不得使用固定靜態門檻
- 動態閾值 = `max(靜態最低值, noise_floor + delta)`
- 串流播放中：閾值拉高到 `RMS_THRESHOLD_STREAM`，避免擴音回聲誤觸發

**對話溫度（Conversation Temperature）**：
- VAD 截斷靜默時間依對話活躍度動態調整：高溫 3.0s / 中溫 1.5s / 低溫 0.8s
- 目的是讓活絡對話時不急著截斷、安靜時快速回應

**最小音訊大小**：切片長度 ≤ 19200 bytes（約 0.1s，48kHz stereo 16-bit）視為雜訊，不送 STT

---

### 音訊播放安全

**TTS 風暴保護**：
- `tts_queue_duration > 10s` 時，新 TTS 改為貼文，不入隊
- `is_playing_audio=True` 時，Echo Guard 降低喚醒靈敏度

**Lock 鏈（不可打破的順序）**：
```
playback_lock → 序列化所有 voice_client.play()
tts_queue_lock → 保護 tts_queue_duration 計數器
_nemo_lock → 序列化 openclaw subprocess + TTS
```

---

### 日誌規範

模組前綴統一：
- Sink 層：`[Core_Sink]`
- STT 層：`[Core_STT]`
- Pipeline 層：`[Core_Pipeline]`
- Voice Controller：`[VC]`

高頻路徑（Sink.write）用 `packet_count % N == 0` 降頻，避免 log 爆炸。

---

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
