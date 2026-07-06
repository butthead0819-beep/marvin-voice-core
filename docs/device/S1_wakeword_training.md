# S1 —「馬文」openWakeWord 模型自訓 runbook

> 目標：產出 `mawen.onnx` + `mawen.tflite`（Pi 上 wyoming-openwakeword 用 tflite）。
> 不需硬體、不需使用者在場（除了最後人耳驗收）。openWakeWord **沒有中文預訓**，必自訓。

## 路線 A（首選）：官方 Colab 自動訓練 notebook
1. 開 https://github.com/dscripka/openWakeWord → `notebooks/automatic_model_training.ipynb` → 在 Google Colab 開（免費 GPU 即可）。
2. 目標詞填拼音形式效果較穩：`"ma wen"`（也試 `"mǎ wén"`、`"marvin"` 各訓一顆比較）。
3. notebook 會：合成 TTS 正樣本 → 混噪音/音樂增強 → 訓小分類頭 → 輸出 `.onnx`/`.tflite`。全照預設跑即可。
4. 產出放 repo `models/wakeword/`（新建目錄），命名 `mawen_v1.onnx` / `mawen_v1.tflite`。

## 路線 B（備選）：openwakeword.com/train 網頁版
若 Colab 卡住：https://openwakeword.com/train 直接填詞產模型（背後同管線）。

## 注入真實樣本（提精度，可選但建議）
`records/wake_samples/` 有 Discord 收集的 owner 真實「馬文」wav（48k stereo）＋ sidecar json。
1. 轉 16k mono：`for f in records/wake_samples/owner_*.wav; do ffmpeg -i "$f" -ac 1 -ar 16000 "${f%.wav}_16k.wav"; done`
2. Colab notebook 有「additional positive samples」欄位 → 上傳這批。
3. ⚠️ 這批是**耳機乾淨音**（train/serve mismatch 已知）：只補「使用者音色」軸，遠場 robustness 靠 notebook 的增強（它會自動混房間脈衝+音樂）。**不要**因為有真樣本就關掉增強。

## 離線驗收（不用耳朵，用檔案）
```bash
# 用收集的真樣本測模型分數（應 >0.5）；用無關語音測（應 <0.2）
/tmp/owwenv/bin/python - <<'PY'
from openwakeword.model import Model
import wave, numpy as np
m = Model(wakeword_models=["models/wakeword/mawen_v1.onnx"], inference_framework="onnx")
for path in ["records/wake_samples/<某個>_16k.wav"]:
    w = wave.open(path); pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    peak = 0.0
    for i in range(0, len(pcm)-1280, 1280):
        peak = max(peak, max(m.predict(pcm[i:i+1280]).values()))
    print(path, "peak score:", round(peak, 3))
PY
```
**驗收標準**：真「馬文」樣本 peak ≥0.5、無關語音（拿 `records/probe_stt_fixture.wav` 轉 16k 測）<0.2。不達標 → 回 Colab 加樣本數/epoch 重訓，連兩次不達標就停下問使用者。

## 最終人耳驗收（使用者在 Mac）
`scripts/wake_over_music_poc.py` 支援自訂模型：改 `Model(wakeword_models=[...])`（見腳本 `--framework` 附近，加參數 `--model models/wakeword/mawen_v1.onnx` 的小改動即可）。音樂中喊「馬文」看 score。
