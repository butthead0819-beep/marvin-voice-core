# Discord Voice -> STT: 技術執行流程與經驗總結

本文記錄了 Marvin 機器人中，語音從 Discord 傳入到轉為文字的完整核心流程，以及開發過程中遇到的坑與解決方案。

## 1. 核心執行流程 (The Pipeline)

目前系統採用的現代化語音流程如下：

### A. 語音接收與解密 (Reception & Decryption)
1.  **Discord Voice Gateway**: 接收 RTP 封包，內含 Opus 編碼的語訊。
2.  **DAVE Decryption**: Discord 最新的加密協議。我們透過 `RealtimeVADSink` 攔截封包，並使用 `davey` 庫（C++ DAVE Client 封裝）進行手動解密。
    *   *關鍵點*：必須在同步完成（`dave_session.ready`）後才進行解密，否則會崩潰。
3.  **Opus Decoding**: 將解密後的 Opus 片段轉為原始 PCM (16-bit, 48kHz, Stereo)。

### B. 語音活動偵測 (VAD - Voice Activity Detection)
1.  **RMS 判定**：即時計算每個封包的 RMS (音量)。只有大於 `RMS_THRESHOLD` (200) 的聲音才被視為「有效人聲」。
2.  **動態溫度計 (Dynamic Pulse)**：透過 `ConversationBuffer` 計算目前的交談頻率，動態調整「靜音截斷閾值」。
    *   聊天熱絡時：允許較長的停頓 (2.0s)。
    *   冷清時：極短回應 (0.8s)。
3.  **看門狗 (Watchdog)**：背景任務不斷檢查：
    *   是否靜音超過閾值？ (觸發切片)
    *   是否說太長了？ (超過 12s 強制切片)
    *   緩衝區是否過大？ (記憶體收割機制)

### C. 音訊處理與校正 (Audio Conditioning)
1.  **自動增益補正 (Normalizer)**：若偵測到 user 音量過小 (RMS < 2500)，程式會自動執行 1.8x 增益，大幅提升 STT 辨識率。
2.  **WAV 聚合**：將 PCM 片段包裝成 48kHz Stereo WAV 暫存檔。

### D. 雙引擎 STT 辨識 (Hybrid Inference)
1.  **第一層：macOS Native Swift STT**
    *   呼叫 `macos_stt.swift`。
    *   優點：極速、離線、支援 `zh-TW`。
    *   黑話注入：透過 `STT_CONTEXT_STRINGS` 注入「Marvin」、「馬文」及當前遊戲術語。
2.  **第二層：Faster-Whisper (Backup)**
    *   若 Swift 辨識失敗，回退至 `tiny` 模型。
    *   *優化*：使用 `beam_size=1` 確保不阻塞 Discord 心跳 (Heartbeat)。

---

## 2. 曾經犯過的錯誤 (Lessons Learned)

### 🚨 導致機器人掉線的核心問題 (Gateway Heartbeat Blocking)
*   **現象**：STT 辨識期間，機器人突然斷開連接 (Error 1006/4014)。
*   **原因**：Whisper 辨識或大型 `audioop` 操作在 `asyncio` 主迴圈中執行，導致無法及時發送 Heartbeat 封包。
*   **教訓**：所有重量級計算必須放在 `asyncio.to_thread` 或單獨進程中（如 Swift 腳本）。

### 🚨 靈異任務殘留 (Ghost Tasks)
*   **現象**：機器人退群或重啟後，依然有 STT 辨識在背景跑，或出現 `AttributeError: 'NoneType' has no attribute 'voice_client'`。
*   **原因**：VAD Watchdog 任務沒有在 disconnect 時被 `cancel()`。
*   **解決**：將任務儲存在 `self._watchdog_task`，在 `stop()` 方法中顯式取消。

### 🚨 背景雜訊導致的「永不間斷」 (Noise Floor Issues)
*   **現象**：某些 User 的麥克風電流聲較大，導致 VAD 永遠不觸發靜音截斷，緩衝區無限長。
*   **改善**：從簡單的 `voice_recv` 事件轉為「硬性 RMS 閾值過濾」，並加入 12 秒強制斷句機制。

### 🚨 DAVE 同步延遲 (Decryption Sync)
*   **現象**：剛進語音頻道的前幾秒，STT 輸出全是亂碼。
*   **原因**：DAVE 金鑰尚未交換完成就嘗試解密。
*   **解決**：加入 `dave_session.ready` 判定，若未就緒則先採取 passthrough 透明傳輸。

---

## 3. 模組解耦 ✅ 已完成 (2026-05-08)

`marvin_voice_core/` 已建立，語音管線完整抽離：

| 檔案 | 職責 |
|---|---|
| `sink.py` | `RealtimeVADSink` — Discord audio 接收、DAVE 解密、VAD 過濾 |
| `pipeline.py` | `MarvinVoicePipeline` + `ConversationBuffer` — 看門狗、切片聚合、動態靜音閾值 |
| `audio_utils.py` | 音量增益補正、WAV 封包 |
| `stt_handler.py` | `STTHandler` — Swift Binary + Faster-Whisper 雙引擎 STT 封裝 |
| `voice_meta_analyzer.py` | `VoiceMetaAnalyzer` — RMS 採集、WPS/Variance 韻律計算 |
| `atmosphere_tracker.py` | `AtmosphereTracker` — 話題標籤 + 情緒狀態，供 GeminiRouter 注入 |
| `marmo_server.py` | `MarmoServer` — Marmo 非同步 webhook 接收器 |

主程式 `main_discord.py` 透過 `from marvin_voice_core import MarvinVoicePipeline, ...` 引入；`VoiceController`（`cogs/voice_controller.py`）保持為薄協調層。
