# S1 — 「hey marvin」openWakeWord 模型自訓 runbook

> 目標：產出 `hey_marvin.onnx`（裝置端喚醒模型）。
> **喚醒詞＝英文「hey marvin」**（2026-07-06 使用者定：裝置自用可接受英文喚醒，且英文
> 詞用英文 TTS 訓超簡單、Discord 的中文「馬文」喚醒完全不受影響——兩條不同 transport）。
> 不需硬體、不需使用者在場（除最後現場驗收）。

## 為什麼是英文「hey marvin」而非中文「馬文」（研究結論，別回頭走冤路）
- **中文超難**：所有簡單訓練器（防彈 Colab / openwakeword.com/train / 標準 piper-sample-
  generator）都用**英文 Piper TTS**、不支援中文、不支援注入真人錄音。中文正統路徑（Kokoro
  TTS 普通話 + 真人錄音）唯一成熟 repo 是 CoreWorxLab/openwakeword-training，但**強制
  NVIDIA CUDA GPU**（Mac M1 跑不了）。
- **英文「hey marvin」則是甜蜜點**：「marvin」是英文名，英文 Piper 乾淨合成，防彈 Colab
  ~90 分搞定。3 音節比裸「marvin」更耐誤觸。
- **Discord 不受影響**：裝置喚醒＝英文詞 + openWakeWord（Pi 衛星端或裝置本地），Discord
  喚醒＝中文「馬文」既有 Swift/regex 路徑，兩條 transport 互不相干。

## 路線（首選）：防彈 2026 Colab notebook
repo：`alfiedennen/openwakeword-colab-2026`（官方 notebook 2026 已 bit-rot 8 個坑，別用官方版）。

1. 開 `train_wakeword.ipynb`（repo 內 Colab badge 或上傳到 Colab）。
2. **Runtime → Change runtime type**：
   - Colab Pro（$10/月）：**L4 GPU + High RAM** → ~75-90 分。
   - 免費層：**T4 GPU** 也能跑，但 ~2.5 小時、且 **tab 別切到背景**（會斷線）。
3. **編輯 Cell 10**（唯一要改的兩行）：
   ```python
   TARGET_PHRASE = ['hey marvin']
   MODEL_NAME    = 'hey_marvin'
   ```
4. **Runtime → Run all**，等它跑完。
5. 最後一格**自動下載 `hey_marvin.onnx`**（sigmoid 已烘進 onnx、~400KB）。放進 repo
   `models/wakeword/`（新建目錄）：`models/wakeword/hey_marvin.onnx`。

## 離線 + 現場驗收（用 `scripts/verify_wake_model.py`，不需硬體）
下載模型後，在 Mac 跑（**owwenv 非 venv_simon**，openwakeword/sounddevice 裝在 owwenv；
owwenv 沒了就 `python3 -m venv /tmp/owwenv && /tmp/owwenv/bin/pip install openwakeword onnxruntime sounddevice numpy`）：
```bash
/tmp/owwenv/bin/python scripts/verify_wake_model.py \
    --model models/wakeword/hey_marvin.onnx \
    --neg records/probe_stt_fixture.wav   # 負樣本可選
```
**驗收標準**（對齊 S0 命門實測）：
- 清楚喊「hey marvin」→ peak ≥ 0.5（安靜下應 0.8+）
- 負樣本無關語音 → peak < 0.2
- 大聲音樂中、喇叭與嘴都約 30cm → 仍多數過 0.5（S0 證英文預訓模型在 ~0 dB SNR 可達 0.83-0.97）

連兩次不達標 → 停下問使用者；或先照下方故障段調。

## 故障 / 調參（Colab 端）
| 症狀 | 調 |
|---|---|
| 召回低（喊了不太觸發，<18/20） | Cell 20 的 `n_samples` 5000（增合成正樣本量） |
| 誤觸多（沒喊也觸發） | `max_negative_weight` 1500 → 3000 |
| 門檻 | 預設 0.5 合理；要更少誤觸拉 0.6–0.7（裝置端 env 調，非重訓） |

## ⚠️ onnx → Pi 的 tflite（S3/S4 才處理，非 S1 blocker）
notebook 只出 **onnx**；Pi 的 wyoming-openwakeword 慣用 **tflite**。兩個選項（S3 時決定）：
1. openWakeWord 自帶 onnx→tflite 轉換工具，轉一顆 `hey_marvin.tflite` 放 Pi `~/wakewords/`。
2. 或設 wyoming-openwakeword 直接吃 onnx（openWakeWord Model 支援 `inference_framework='onnx'`）。
S3 runbook 的 `--wake-word-name` 對應改成 `hey_marvin` 即可。

## 真人錄音注入（可選 v2，非 v1 必要）
防彈 notebook **不支援**注入真人錄音。v1 純 TTS 合成的「hey marvin」通常已夠（S0 證英文
預訓模型免真人樣本就很穩）。若 v1 現場驗收在你的口音下召回不佳，才考慮 v2：換 Kokoro 路徑
（要雲端 GPU）注入你錄的 20-50 段「hey marvin」。`records/wake_samples/` 那批是含「馬文」
的**整句**、非喚醒詞短 clip，不能直接當正樣本（要用另外乾淨錄的）。
