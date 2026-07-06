#!/usr/bin/env python3
"""S1 驗收：測自訓的 openWakeWord 模型（如 hey_marvin.onnx）好不好。

Colab 訓完下載 `hey_marvin.onnx` 後，用這支在 Mac 上驗收——**不需硬體**（用內建麥），
沿用 S0 命門測法：喊喚醒詞看分數過不過門檻、放音樂再喊看耐不耐遮蔽。

用法（跑在 owwenv，非 venv_simon——openwakeword/sounddevice 裝在 owwenv）：
  /tmp/owwenv/bin/python scripts/verify_wake_model.py --model /path/to/hey_marvin.onnx

  # 離線負樣本測（可選）：拿無關語音驗「不該觸發」
  /tmp/owwenv/bin/python scripts/verify_wake_model.py --model hey_marvin.onnx \
      --neg records/probe_stt_fixture.wav

驗收標準（對齊 docs/device/S1_wakeword_training.md）：
  - 清楚喊「hey marvin」→ peak score ≥ 0.5（安靜下應 0.8+）
  - 負樣本無關語音 → peak score < 0.2
  - 大聲音樂中喊（近場 30cm）→ 仍多數過 0.5（S0 實測英文預訓模型可達）
不達標 → 回 Colab 加 n_samples / 調 max_negative_weight（見 S1 runbook 故障段）。
"""
from __future__ import annotations

import argparse
import time
import wave

import numpy as np

MIC_RATE, MIC_FRAME = 16000, 1280   # openWakeWord 要 16k mono、80ms 幀


def _offline_peak(model, wav_path: str) -> float:
    """對一個 16k mono wav 掃一遍，回最高分（負樣本驗收用）。"""
    w = wave.open(wav_path)
    if w.getframerate() != MIC_RATE or w.getnchannels() != 1:
        print(f"⚠️  {wav_path} 非 16k mono（rate={w.getframerate()} ch={w.getnchannels()}），"
              "先 ffmpeg -ac 1 -ar 16000 轉檔", flush=True)
    pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    peak = 0.0
    for i in range(0, len(pcm) - MIC_FRAME, MIC_FRAME):
        peak = max(peak, max(model.predict(pcm[i:i + MIC_FRAME]).values()))
    return peak


def main() -> None:
    ap = argparse.ArgumentParser(description="自訓 openWakeWord 模型驗收")
    ap.add_argument("--model", required=True, help="自訓模型路徑（.onnx）")
    ap.add_argument("--neg", default=None, help="負樣本 wav（16k mono，應 <0.2）")
    ap.add_argument("--threshold", type=float, default=0.5)
    args = ap.parse_args()

    from openwakeword.model import Model
    model = Model(wakeword_models=[args.model], inference_framework="onnx")
    print(f"✅ 載入自訓模型：{list(model.models.keys())}", flush=True)

    if args.neg:
        peak = _offline_peak(model, args.neg)
        verdict = "✅ PASS（<0.2）" if peak < 0.2 else "❌ FAIL（誤觸，應 <0.2）"
        print(f"📄 負樣本 {args.neg} peak={peak:.3f}  {verdict}", flush=True)

    import sounddevice as sd
    print("🎧 現場驗收：喊「hey marvin」。安靜先喊幾次、再放音樂喊。Ctrl-C 結束。", flush=True)
    win_start, win_max, win_name, last_wake = time.monotonic(), 0.0, "", 0.0
    with sd.InputStream(samplerate=MIC_RATE, channels=1, dtype="int16", blocksize=MIC_FRAME) as mic:
        try:
            while True:
                frame, _ = mic.read(MIC_FRAME)
                scores = model.predict(frame.flatten())
                name, score = max(scores.items(), key=lambda kv: kv[1])
                now = time.monotonic()
                if score > win_max:
                    win_max, win_name = score, name
                if score >= args.threshold and now - last_wake > 2.0:
                    print(f"🔔 WAKE  {name}  score={score:.2f}", flush=True)
                    last_wake = now
                if now - win_start >= 2.0:
                    tag = "🔊" if win_max >= args.threshold else ("·" if win_max < 0.2 else "…")
                    print(f"{tag} 2s區間 max: {win_name} {win_max:.2f}", flush=True)
                    win_start, win_max, win_name = now, 0.0, ""
        except KeyboardInterrupt:
            print("\n👋 驗收結束。", flush=True)


if __name__ == "__main__":
    main()
