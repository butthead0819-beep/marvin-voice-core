# Marvin Mobile: Music AI Agent 產品與技術架構計畫書
> **版本**：v1.0 (Draft for Claude & Team Review)  
> **建立日期**：2026-08-21  
> **定位**：跨串流平台（Spotify / Apple Music）的個人化毒舌音樂電台 DJ 伴侶

---

## 1. 執行摘要 (Executive Summary)

### 1.1 核心問題 (Problem Statement)
* **聽歌疲勞**：現代人戴耳機聽歌容易陷入演算法同溫層，想聽新歌卻常踩雷或聽到重複曲目（渴望「Surprise Me」）。
* **官方 AI DJ 無趣**：Spotify 官方 AI DJ 走公關正能量腔、無中文在地靈魂；Apple Music 至今無語音 DJ 主播。
* **單人聽歌缺乏社交連結**：耳機聆聽是孤獨的體驗，缺乏像 Discord 語音房同樂、互相吐槽的社交驚喜。

### 1.2 產品願景 (Core Value Proposition)
將 Marvin 現有的「毒舌/幽默人格 + 智慧挑歌 + DJ 口白生成系統」轉化為 Mobile App：
1. **Surprise Me 智慧選歌**：專注挖掘冷門寶藏與跨曲風轉換，打破演算法同溫層。
2. **黃金比例口白串場**：音樂為主菜、口白為調味料（每 3~5 首插播 8~12 秒），具備分級制毒舌/生活吐槽。
3. **零版權、零音訊頻寬負擔（BYOM 模式）**：使用者自備 Spotify / Apple Music 會員，App 只扮演「大腦控制與語音插播」。
4. **社交驚喜與變現**：支援「非同步語音彩蛋（送歌給朋友並在前奏突襲留言）」與「KOL 嘲諷台詞語音包」。

---

## 2. 市場現況與競品分析 (Market Landscape)

| 維度 | Spotify 官方 AI DJ (DJ "X") | 傳統 DJ App (Mixonset / Algoriddim) | **Marvin Mobile** |
| :--- | :--- | :--- | :--- |
| **平台支援** | 綁死 Spotify | 支援 Spotify / 本機 | **Spotify + Apple Music 雙支援** |
| **口白與人設** | 正式、客套、公關腔、缺乏中文靈魂 | 無口白（純對拍混音） | **台灣口語、毒舌吐槽、自發漫才、鮮明人設** |
| **互動與社群** | 僅能點擊切換 Vibe | 無社群 | **非同步朋友語音彩蛋 + KOL 語音包商城** |
| **尺度分級** | 全年齡通用（極保守） | 無 | **年齡牆分級（Level 1 溫柔 ~ Level 3 Grok 地獄級）** |

> **華語市場關鍵認知**：KKBOX 目前僅提供公開 Metadata API，**無公開 Playback SDK**；因此 MVP 階段聚焦於覆蓋台灣付費市場最大宗的 **Apple MusicKit** 與 **Spotify App Remote SDK**。

---

## 3. 系統架構與技術方案 (System Architecture)

### 3.1 整體架構圖 (Client-Server Hybrid)

```mermaid
graph TD
    subgraph Mobile App (Client - iOS / Android)
        A1[Music SDK Controller: Spotify App Remote / Apple MusicKit]
        A2[Audio Ducking Engine: AVAudioSession.duckOthers]
        A3[On-Device TTS / Native Voice: Kokoro-82M CoreML / AVSpeechSynthesizer]
        A4[UI & Interaction: 播放器、點擊說話 PTT、驚喜彩蛋]
    end

    subgraph Marvin Cloud Core (Server - FastAPI)
        B1[API Gateway & User Session]
        B2[DJ Director: Surprise Me 排歌演算法]
        B3[DJ Banter: 口白生成 Engine + 嘲諷等級控制]
        B4[Social Surprise Engine: 非同步語音彩蛋佇列]
        B5[User Context DB: 聽歌偏好 / 歷史 / 社交關係]
    end

    A1 -- 歌曲即將結束前 10 秒通知 --> B1
    B1 --> B2 & B3
    B3 -- 回傳口白純文字 JSON --> A3
    A3 -- 本機生成語音並播放 --> A2
    A2 -- 壓低音樂音量 70% 播出口白 --> A1
```

### 3.2 關鍵技術決策 (Key Technical Decisions)

#### ① 音樂播放與混音：Client 端 Audio Ducking（絕不自建串流伺服器）
* **方案**：音樂由系統背景的 Spotify / Apple Music 播放（Track A）；Marvin 口白由 App 本機獨立音軌播放（Track B）。
* **機制**：利用 iOS 原生 `AVAudioSession.CategoryOptions.duckOthers`，在口白開始時自動將音樂音量降至 20~30%，口白結束後平滑恢復 100%。
* **優勢**：**零音樂版權問題、零音樂伺服器頻寬費、符合平台開發者條款**。

#### ② 語音合成（TTS）成本歸零方案：On-Device 推論
* **問題**：若走 ElevenLabs 等商業 API，每人每月 TTS 費用高達 NT$ 150~300，商業模式無法打平。
* **解法**：
  * **Phase 0/1 (MVP)**：調用 iOS 系統內建 `AVSpeechSynthesizer` (Enhanced zh-TW 美嘉/Siri 語音)，API 成本 $0。
  * **Phase 2 (正式版)**：整合 **Kokoro-82M CoreML**（或 Sherpa-onnx），App 內建 ~80MB 輕量模型，由 iPhone NPU (Neural Engine) 於本機進行神經網絡 TTS。
* **伺服器負載**：Server 只需回傳純文字台詞（單次 <100 bytes），伺服器頻寬與算力成本趨近於零。

#### ③ 隱私與語音互動：放棄背景全時監聽，改走 PTT 與情境窗口
* **原則**：禁止背景常駐麥克風（避免 iOS 橘點警告與耗電殺進程）。
* **互動模式**：
  1. **80% 被動聆聽**：廣播電台模式，純聽歌與定時口白，完全不開麥克風。
  2. **Push-to-Talk (PTT)**：用戶點擊按鈕或線控長按時錄音 3 秒發送指令。
  3. **Turn-taking 短窗口**：Marvin 口白提問時（例如「*這首要聽嗎？*」），短暫開啟 4 秒麥克風等待回應。

---

## 4. 產品節奏與內容分級 (Product Experience & Content Rating)

### 4.1 口白出現黃金節奏
* **原則**：音樂是主菜，口白是調味料。嚴禁每首歌都插播。
* **觸發時機**：
  * **每 3~5 首歌曲之間**（或間隔約 12~15 分鐘）。
  * **風格/曲風大轉折時**（例如從抒情轉搖滾，DJ 進行情緒鋪墊）。
  * **用戶連續 Skip 時**（觸發吐槽機制：「*連跳三首了，你今天到底有多挑剔？*」）。
  * **有朋友發送的非同步語音彩蛋時**。

### 4.2 內容尺度與年齡牆 (Age Gate)
* **App Store 評級**：設定為 **17+**（包含強烈幽默、冒犯性言語、AI 生成內容）。
* **App 內嘲諷強度切換（Personality Sliders）**：
  * **Level 1 (溫和)**：音樂導聆、歌手背景介紹、正面陪伴。
  * **Level 2 (傲嬌毒舌 - 預設)**：熟朋友互嘴、品味調侃、生活日常吐槽。
  * **Level 3 (Grok/地獄模式 - 需二次年齡確認)**：無禮貌限制、社畜暴躁發洩、地獄梗齊發（但底線攔截仇恨言論與自殘暴力）。

---

## 5. 社群機制與商業模式 (Social Growth & Monetization)

```mermaid
graph TD
    M[獲利途徑 Monetization]
    M --> M1[Freemium 訂閱制]
    M --> M2[KOL 語音包商城 IAP]
    M --> M3[社交彩蛋付費道具]

    M1 -->|免費版| F1[基礎 Surprise 挑歌 + Level 1/2 口白]
    M1 -->|Pro 版 NT$ 60-90/月| F2[Level 3 地獄模式 + 無限語音彩蛋 + 個人音樂週報]
    M2 -->|單次購買 NT$ 30-60| F3[知名實況主 / 迷因 KOL 專屬吐槽語音包 (五五分潤)]
```

### 5.1 社交病毒傳播機制 (Virality Engine)
1. **「非同步語音彩蛋（Voice Bomb）」**：
   * 用戶 A 在 App 內選擇一首歌曲，錄製 5 秒語音，產生邀請連結給用戶 B。
   * 用戶 B 在聽歌過程中無預警收到音樂壓低 + A 的語音突襲，激發分享至 IG / Threads 的動力。
2. **社群切片行銷 (Organic Shorts Marketing)**：
   * 錄製「聽歌時突然被 AI DJ 嗆爆」的真實反應影片，在 TikTok、Reels、Threads 投放/引流。

---

## 6. 單元經濟模型 (Unit Economics)

以單一用戶每天聽歌 3 小時為例：

| 項目 | 傳統雲端架構 (ElevenLabs + Cloud LLM) | **本案優化架構 (On-Device TTS + Fast LLM)** |
| :--- | :--- | :--- |
| **TTS 費用** | ~NT$ 6.0 / 天 (ElevenLabs) | **NT$ 0 / 天 (手機本機推論)** |
| **LLM 費用** | ~NT$ 0.5 / 天 (GPT-4o) | **~NT$ 0.05 / 天 (Gemini Flash / DeepSeek)** |
| **音訊頻寬** | ~NT$ 1.5 / 天 (雲端音訊串流) | **NT$ 0 / 天 (純文字 JSON 傳輸)** |
| **單月單人成本** | **約 NT$ 240 / 月** ❌ (賠錢) | **約 NT$ 1.5 ~ 3.0 / 月** ✅ (毛利 >95%) |

---

## 7. 推進路線圖 (Roadmap)

### Phase 0: 極速 POC（預計 1 週）
* [ ] 建立 Python 驗證腳本：測試「音樂播放中 → 即將結束前 10 秒觸發 LLM Banter → 透過 Ducking 壓音播放口白 → 切歌」。
* [ ] 評估 iOS `AVSpeechSynthesizer` vs `Kokoro-82M CoreML` 在繁體中文語境下的聽感與延遲。

### Phase 1: Headless Core 抽取與 API 化（預計 2 週）
* [ ] 從 Marvin 專案抽離 `dj_director.py` 與 `dj_banter.py` 為獨立 FastAPI 模組。
* [ ] 定義 Client-Server 通訊協定（JSON-RPC / REST）。
* [ ] 實作三檔嘲諷強度（Level 1~3）之 System Prompts 與安全過濾層。

### Phase 2: iOS Client 原型與核心功能整合（預計 3 週）
* [ ] 整合 Apple MusicKit 與 Spotify App Remote SDK 遙控播放。
* [ ] 實作雙軌 Audio Ducking 與本機 TTS 引擎。
* [ ] 實作「非同步語音彩蛋」接收與插入播放邏輯。

### Phase 3: 封測、社群裂變與上架（預計 3 週）
* [ ] 啟動 TestFlight 50 人封閉測試（重點測試斷線重連與聽感節奏）。
* [ ] 製作 Threads / TikTok 實測切片進行病毒式行銷。
* [ ] 提交 App Store 審查（設定 17+ 年齡分級與 AI 生成內容條款）。

---

## 8. 風險與應對對策 (Risks & Mitigations)

| 風險項目 | 影響程度 | 應對策略 |
| :--- | :--- | :--- |
| **Spotify SDK 背景斷線** | 中 | 優先以 Apple MusicKit 為主推體驗（系統級支援最穩定）；Spotify 端加入 Heartbeat 喚醒與重連機制。 |
| **毒舌內容踩審查紅線** | 高 | App Store 勾選 17+ 分級；後端強制過濾 Hate Speech / 暴力威脅；提供用戶一鍵檢舉與關閉毒舌功能。 |
| **冷門歌曲不合用戶胃口** | 中 | 建立即時 Skip 反饋迴圈：若一首歌在 30 秒內被 Skip，演算法即刻退回相鄰安全曲風，並由 DJ 口白自嘲打圓場。 |
