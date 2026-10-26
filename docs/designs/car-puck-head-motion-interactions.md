# Design: ESP32 Car Puck Marvin Head 視覺注意場景與動作演出規格

- Status: READY FOR IMPLEMENTATION
- Target Components:
  - `firmware/car_puck/face_motion.h` (純 C++ 姿態與動作狀態機，host 可測)
  - `firmware/car_puck/car_puck.ino` (ESP32-S3 FreeRTOS faceTask、PTT 與感測輸入)
  - `firmware/car_puck/test/test_face_motion.cpp` (Host C++ 測試集)
  - `puck_gesture.py` / `tests/test_puck_gesture.py` (Mac 端語音/意圖動作連動)
- Branch: main
- Priority: P1

---

## 一、 背景與設計哲學（Problem Statement & Philosophy）

### 1.1 行車視覺的極限約束
ESP32 Car Puck 配備 GC9A01 圓屏（240×240），安裝於車載中控出風口或排檔座周圍。
- **視線極短（Glance Duration）**：駕駛視線主體必須留在道路前方，只有在**「主動操作按鍵」**、**「聽覺定向反射（聽到聲音轉頭）」**或**「系統狀態突變（卡頓/斷線）」**時，才會以餘光或轉頭掃描 Puck **0.5～2.0 秒**。
- **無微表情空間**：在 32mm 小圓屏與震動的行車環境中，精緻細微的面部表情駕駛完全無法識別。必須依靠**大輪廓的三軸角度（Pitch 俯仰、Yaw 偏航、Roll 側傾）**與**眼球/眉線/錯誤光芒**來表達意圖。
- **拒絕無效閃爍**：夜間駕駛環境下，突兀的瞬移或高頻抽動會造成餘光干擾與行車危險；所有轉場必須維持在彈簧阻尼系統（Spring-Damper）與平滑漸層（$FADE\_S = 0.4s$）中。

### 1.2 Marvin 的人設靈魂
Marvin 是一台**厭世、聰明、傲嬌且消極怠工**的機器人：
- 他會專心聽你說話（因為職責所在）；
- 他在思考時會仰頭算計怎麼諷刺你；
- 如果雲端大腦卡頓，他會無奈斜眼看著你（「別看我，網路在塞車」）；
- 長時間沒人理他，他會無聊打呵欠、放空打瞌睡；
- 聽到好聽的歌或快歌，他身體會誠實地踩拍點頭（`LAYER_NOD`）。

---

## 二、 視覺注意場景矩陣（Attention Trigger Scenarios）

當駕駛的目光落到 Puck 上時，Marvin Head 必須在該瞬間呈現對應的姿態反饋：

| 編號 | 場景分類 | 觸發時刻 (Trigger) | 駕駛心理預期 (Expectation) | Marvin 動作演出 (Performance) | 關鍵參數 / 通道變更 |
|---|---|---|---|---|---|
| **S1** | **主動操作** | 按下 PTT 開始錄音 | 「你有在聽我說話嗎？」 | **側耳傾聽（`LAYER_LISTEN`）+ 眼神朝駕駛對視 + 說話音量微彈跳** | Yaw 偏向駕駛座（左駕 -12°），眉線隨麥克風即時 RMS 音量微幅跳動 |
| **S2** | **主動操作** | 放開 PTT 等候回應 (0~3s) | 「有收到嗎？大腦在動嗎？」 | **轉向天花板算計沉思（`LAYER_THINK`）** | Pitch -0.3, Yaw +0.45（視線避開駕駛，仰望上方） |
| **S3** | **異常焦慮** | 放開 PTT 等候逾時 (3~10s) | 「當機了嗎？要不要重按？」 | **焦躁不耐煩（`LAYER_ANNOYED` / Await Impatience）** | 從仰望慢慢轉為「眼神斜視駕駛座（Yaw 左偏、眉毛挑高 0.08）」，無奈等待 |
| **S4** | **聽覺引發** | Marvin 開口說第一句話 | 「誰在講話？講話的情緒是什麼？」 | **開口動作標籤（Gesture T/Y/N/G/P）+ 說話高頻微震（`LAYER_SPEAK`）** | 依首句情感套用 3 檔振幅（0.6/1.0/1.3），隨後回正並微幅上下震動 |
| **S5** | **聽覺引發** | 音樂換曲前奏進歌 | 「換了什麼歌？」 | **換歌歪頭（`GESTURE_TILT`）➔ 圓屏徽章封面 (6s) ➔ 新曲 BPM 踩拍** | 換歌 0.5s 歪頭挑眉，淡出進 240px 封面；淡回時無縫切入 `LAYER_NOD`/`SWAY` |
| **S6** | **環境情境** | 剛上車發動 / 開機通電 | 「Marvin 醒了嗎？在線上了嗎？」 | **開機甦醒序曲（Boot Wake-up Sequence）** | 雙眼暗黑 ➔ 漸亮（暗綠到亮綠）➔ 低頭緩慢抬起 ➔ 環顧四周後定焦 |
| **S7** | **環境情境** | 停等紅燈 / 長時間無對話 | 「機器人死了嗎？還是循環動畫？」 | **摸魚與瞌睡（Idle Fidgeting & Drowse）** | 無歌待機超過 45 秒：隨機打哈欠（低頭緩閉眼再揉眼）、偷看窗外再看駕駛 |
| **S8** | **環境情境** | 熄火下車 / 離車中斷 | 「下班了」 | **熄火休眠（Sleep / Power Down）** | 頭部緩緩垂下（Pitch +0.4），雙眼光芒逐漸淡出熄滅 |
| **S9** | **系統異常** | 斷線 / 致命錯誤 | 「怎麼沒聲音了？」 | **錯誤警戒（`LAYER_ERROR`）** | 雙眼泛紅（err=1.0），劇烈左右搖頭（Yaw 0.7 振幅震盪） |

---

## 三、 技術規格與實作清單（Technical Specifications）

### 3.1 駕駛座視角偏置（Driver Orientation Offset）
- **物理現實**：在台灣車輛（左駕 LHD），Puck 置於中控台或右側出風口時，相對於駕駛座有約 **-10° ～ -15° 的水平視角差**。
- **規格**：
  在 `firmware/car_puck/face_motion.h` 中定義左駕偏角偏移：
  ```cpp
  constexpr float DRIVER_YAW_BIAS = -0.18f; // 約 -10.3 度，偏向左側駕駛座
  ```
  在 `layerStill()`、`layerSpeak()` 以及基準中立姿態疊加此偏角，使駕駛瞄向 Puck 時，Marvin 有「直視駕駛眼神」的強烈對視感。

---

### 3.2 S1: PTT 麥克風音量微彈跳（Mic Audio RMS Activity）
- **現況**：`layerListen(t)` 只有時間正弦波，沒有麥克風動態。
- **規格**：
  1. `car_puck.ino` 在 PTT 按住錄音迴圈中，計算 INMP441 錄音緩衝區的 RMS（0 ~ 1000）。
  2. 映射至標準化浮點數 `micNorm = clamp01((rms - 30.0f) / 300.0f)`。
  3. 修改 `layerListen`：
     ```cpp
     inline Pose5 layerListen(float t, float micNorm = 0.0f) {
       float c = fmodf(t, 1.8f);
       if (c < 0) c += 1.8f;
       float b = c < 0.7f ? sinf(3.14159265358979f * c / 0.7f) : 0.0f;
       b = b * b;
       // micNorm 帶來微幅的點頭傾聽與挑眉回饋（最大 Pitch +0.12, browUp +0.08）
       float nod = 0.12f * micNorm;
       float brow = 0.08f * micNorm;
       return {-0.35f + DRIVER_YAW_BIAS, -0.06f + 0.1f * b + nod, 0.34f, brow, 0};
     }
     ```

---

### 3.3 S2 & S3: 放開 PTT 等候回應的二階段姿態（Await Reply Impatience）
- **現況**：`snap.awaitUntilMs` 期間一律顯示 `LAYER_THINK`（仰天長思），直到 10 秒超時。超過 3 秒時駕駛會焦慮不知道是否當機。
- **規格**：
  新增姿態層 `LAYER_ANNOYED`（焦躁斜眼），並於 `resolve_override` 依等候耗時切換：
  ```cpp
  inline Pose5 layerAnnoyed(float t) {
    // 眼神轉向駕駛座（Yaw 偏左），頭微偏，眉毛微挑，微幅不耐煩抖動
    float fidget = 0.04f * sinf(PI2 * t / 1.2f);
    return {-0.40f + DRIVER_YAW_BIAS + fidget, -0.10f, 0.18f, 0.08f, 0};
  }
  ```
  在 `faceTask` 中判斷等候時間：
  - 放開 PTT 0.0s ～ 3.0s：套用 `OVERRIDE_THINK`。
  - 放開 PTT 3.0s ～ 10.0s：套用 `OVERRIDE_ANNOYED`（提示大腦延遲中，但仍在等待）。
  - 超過 10.0s：清空等候，回退一般姿態。

---

### 3.4 S7: 長時間待機摸魚與瞌睡（Idle Drowse & Wake）
- **現況**：`face_idle_step` 在沒歌時，只會每隔 20~40 秒呼叫一次 `GESTURE_GLANCE`。
- **規格**：
  在 `face_idle_step` 中擴充：
  1. 當 `!in.playing` 且 `idleSinceSec > 45.0f` 時，進入「瞌睡序列（Drowse）」：
     - 頭部緩慢前傾（Pitch 漸變至 +0.35）。
     - 眼部光線降低（`rawBl` 降至 30% 呼吸微光）。
  2. 當任一新事件發生（PTT 按下、新歌進播、開口說話），瞬間觸發 0.2s 的「驚醒反應（Startle）」：
     - Pitch 瞬間拉回 -0.1，眉毛抬高 0.12，隨後進入目標姿態。

---

### 3.5 S6: 開機甦醒序曲（Boot Sequence）
- **現況**：開機後 `faceTask` 直接進入 `LAYER_STILL`。
- **規格**：
  在 `faceTask` 初始化前 2.5 秒定義開機動畫：
  - $T \in [0, 0.8s]$：螢幕背光從 0 緩升至正常值，頭部低垂（Pitch 0.45）。
  - $T \in [0.8s, 1.8s]$：抬頭並朝左右環顧（Yaw -0.5 ➔ +0.5）。
  - $T \in [1.8s, 2.5s]$：目光回正偏向駕駛座，眨眼一次，進入正常待機。

---

## 四、 測試規格與驗收條件（TDD & Verification Plan）

### 4.1 Host C++ 單元測試（`firmware/car_puck/test/test_face_motion.cpp`）
測試必須能在 macOS / Linux host 端以 clang/g++ 獨立編譯並快速通過（零 heap、純邏輯）：
1. **`test_driver_bias`**：驗證 `layerStill()` 與 `layerSpeak()` 在靜止狀態下之 yaw 偏角確實包含 `DRIVER_YAW_BIAS`。
2. **`test_listen_mic_activity`**：驗證 `layerListen(t, micNorm=0.0)` 與 `layerListen(t, micNorm=1.0)` 的 pitch 與 browUp 具有階梯性增量。
3. **`test_await_impatience_transition`**：驗證等候回應狀態下，前 3 秒解析為 `LAYER_THINK`，超過 3 秒後解析為 `LAYER_ANNOYED`。
4. **`test_idle_drowse_state`**：驗證長時間沒歌且超過 45 秒時，idle 姿態會沉降至瞌睡 pitch。

**執行驗證指令**：
```bash
c++ -std=c++17 -Wall -Wextra -O1 -fsanitize=undefined -fno-sanitize-recover=all \
  -o /tmp/test_face_motion firmware/car_puck/test/test_face_motion.cpp && /tmp/test_face_motion
```

### 4.2 Python 單元測試（`tests/test_puck_gesture.py`）
1. 確保現有 316 行測試無任何回歸（Regressions）。
2. 驗證 ICY 第 11 欄在各種文本開頭能正確產生情緒等級與手勢代碼。

**執行驗證指令**：
```bash
source venv_simon/bin/activate && python3 -m pytest tests/test_puck_gesture.py -q
```

---

## 五、 給 Claude Code 的任務交接指令（Prompt for Sonnet / Claude Code）

複製下方指令，直接發包給 Claude Code 執行：

```bash
claude -p --model sonnet --permission-mode acceptEdits "$(cat << 'EOF'
請閱讀 docs/designs/car-puck-head-motion-interactions.md 的詳細規格，並依序完成以下任務：

1. 遵循嚴格 TDD 流程：先在 firmware/car_puck/test/test_face_motion.cpp 新增測試案例（驗證 DRIVER_YAW_BIAS、layerListen micNorm 反應、layerAnnoyed 姿態、等候回應 3 秒切換邏輯）。
2. 執行編譯測試確認測試全紅：
   c++ -std=c++17 -Wall -Wextra -O1 -fsanitize=undefined -fno-sanitize-recover=all -o /tmp/test_face_motion firmware/car_puck/test/test_face_motion.cpp && /tmp/test_face_motion
3. 實作 firmware/car_puck/face_motion.h：
   - 增加 DRIVER_YAW_BIAS 常數並融入相關姿態。
   - 更新 layerListen 加入 micNorm 調製。
   - 新增 LAYER_ANNOYED 與 OVERRIDE_ANNOYED。
   - 更新 resolve_override 支援 await 超過 3 秒時進入 ANNOYED。
4. 再次編譯並執行 host 測試，確保全綠零 warning。
5. 接線 firmware/car_puck/car_puck.ino：
   - 在 faceTask 中計算 await 經過時間，傳入 faceInputs。
   - 將錄音時的 RMS 標準化值傳遞至 snapshot。
6. 執行 Python 側測試確保既有功能無回歸：
   source venv_simon/bin/activate && python3 -m pytest tests/test_puck_gesture.py -q
7. 完成後總結修改行數與驗收結果。
EOF
)"
```
