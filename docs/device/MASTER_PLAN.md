# Marvin 實體音箱 — 執行總綱（給接手的模型/人）

> 2026-07-06 由強模型窗口規劃。**執行者假設：較弱的模型 + 不在場的使用者。**
> 鐵則：**照 runbook 做、每步驗收、卡住就停下來問使用者，不要自由發揮。**

## 你在哪裡（現況，全部已 land 進 main）
- ✅ off-Discord 本機迴圈端到端通（mic→VAD→STT→IntentBus→TTS→喇叭，`main_local.py`）
- ✅ VAD：自適應底噪 + 1.5s 時間切句（`marvin_voice_core/local_mic_sink.py`）
- ✅ wake-duck：喚醒即壓音樂（`LocalMixingAudioSource.duck_for_wake`，Discord live）
- ✅ **S2 Wyoming 橋已寫好+測綠**（`marvin_voice_core/wyoming_bridge.py`，7 測）＝Pi 衛星接腦的那塊
- ✅ **S4 整合 code 全鋪好+測綠+land**（commit b2eda60）＝身分映射 `_resolve_speaker_name`
  ／播放 adapter `wyoming_speaker_output.py`／入口 `start_satellite_listening`+`main_satellite.py`
  （+22 測）。硬體到貨後 S4 只剩「設 .env → 跑 main_satellite.py → 走驗收天梯」，弱模型不用 author code。
- ✅ 喚醒音檔收集中（`records/wake_samples/`，Discord owner 喚醒自動存，已 7 筆）
- ✅ 硬體已下單（DigiAMP+ / ReSpeaker XVF3800 / 12-24V 電源 / microSD；Pi 3B 使用者已有）

## Step 地圖與順序
| Step | 內容 | 前置 | runbook |
|---|---|---|---|
| S0 | Mac 命門 POC（音樂中喚醒） | 使用者在 Mac | ✅ 已做（綠燈，見下方+記憶） |
| S1 | **「hey marvin」**openWakeWord 自訓（英文，非中文馬文） | 無（隨時可做） | `S1_wakeword_training.md` |
| S3 | Pi 設定（OS/DigiAMP+/satellite） | 硬體到貨 | `S3_pi_setup.md` |
| S4 | 整合點火（橋接腦、duck、身分） | S3 完成 | `S4_integration.md` |
| S5 | 存在感層（外殼/LED/PTT） | S4 通 | 本檔下方 |

S1 與 S3 可平行；S0 隨使用者回 Mac 就做（不擋 S3/S4，但擋「聲學調參」判斷）。

## 通用鐵則（違反=停）
1. **改 code 後必跑** `venv_simon/bin/python -m pytest -q`（在 worktree `/Users/jackhuang/Code/dvb-phase1`，venv 在主 repo）。**全綠才可 commit。**
2. **Discord 生產路徑不可回歸**：不動 `discord_voice_engine.py` 的 Discord 分支、`cogs/` 的 Discord gate，除非 runbook 明寫。
3. `cogs/voice_controller.py` 有 **size 棘輪 4285 行**（`test_voice_controller_size_budget`）——新邏輯放新模組，別塞進去。
4. land 流程：worktree commit → **cd 到主 repo** `/Users/jackhuang/Code/Discord-voice-bot` 再 `git merge --ff-only phase1-local-transport && git push origin main`（在 worktree merge 是 self-merge 無效）。
5. 生產重啟：台上無人才可 `launchctl kickstart -k gui/$(id -u)/com.antigravity.marvin.bot`；20-24 點非緊急不重啟。
6. 不確定 → 問使用者，別猜。

## S0：Mac 命門 POC（✅ 2026-07-06 已做，綠燈）
> 實測結論：命門真變數是**人聲到麥克風的相對強度（使用者距離/近場）**，不是音樂。喇叭與
> 嘴都控 30cm、音樂 ~0 dB SNR 下，英文預訓模型 6/6 全中 0.83-0.97＝等同靜音。背書 XVF3800
> 遠場波束 + 門檻維持 0.5。真命門收斂到 S1 模型品質。細節見記憶 project_marvin_physical_speaker。
> 以下為原始 runbook（重跑或換模型驗收時用）：

### （原始 S0 步驟）Mac 命門 POC（使用者在 Mac 時，30 分鐘）
```bash
# venv（/tmp/owwenv 可能還在；沒了就重建）
python3 -m venv /tmp/owwenv && /tmp/owwenv/bin/pip install openwakeword onnxruntime sounddevice numpy
/tmp/owwenv/bin/python scripts/wake_over_music_poc.py --music <大聲的歌.mp3>
```
音樂放著喊 `alexa` / `hey jarvis`。**驗收**：印 `🔔 WAKE`＋音樂沉 3s。**解讀**：比較「靜音 vs 音樂大聲」時的 score 差＝命門嚴重度。iPhone 測已證「方向 OK、SNR 是牆」；此步驗 openWakeWord（音樂混訓）是否比 Swift wake 更耐。結果貼給使用者判斷。

## S5：存在感層（S4 通了才做，機械工作）
- 外殼：木盒/現成盒，Pi+DigiAMP+ 疊裝（HAT 直插 GPIO），XVF3800 USB 麥朝上頂部開孔。
- 呼吸 LED（NeoPixel 環）：抄 `OHF-Voice/linux-voice-assistant` 的 LED/按鈕 websocket 週邊模式；狀態=閒/聽/想/說四態、斷線滅光（對 liveness 誠實）。
- PTT 鈕：GPIO 按鈕；按下→直接觸發 satellite 串流（wyoming-satellite 支援 event/wake bypass）。SNR 極端場景的兜底。
- 喇叭線接 DigiAMP+ 螺絲端子（L+/L-/R+/R-），12-24V barrel 供電（**Pi 不插自己的 USB 電源**，HAT 併供）。

## 驗收天梯（每過一階回報使用者）
1. S3：Pi 開機、`aplay` 經 DigiAMP+ 出聲、`arecord` 收到 XVF3800 音訊
2. S3：wyoming-satellite 跑起來、Mac `nc -z pi.local 10700` 通
3. S4：橋連上、Mac log 出現 `🛰️ [WyomingBridge] 已連上衛星`
4. S4：對 Pi 麥講話 → Mac STT log 有字（`✅ [STT Output]`）
5. S4：喊喚醒詞 → Marvin 回話從**書架喇叭**出來（端到端）
6. S4：放音樂中喚醒 → duck → 回話 → 音樂恢復（＝整案完成）

## 相關記憶（開工前讀）
`project_marvin_physical_speaker`（架構決策史）/ `reference_physical_speaker_github_parts`（零件+輪子）/ `project_identity_unification`（身分映射）/ `project_apple_edge_fleet_direction`（終局，parked）
