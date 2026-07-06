#!/usr/bin/env python3
"""Step 0 命門原型：openWakeWord 在「音樂中」偵測喚醒詞 → duck 音樂確認。

驗實體音箱的命門——「音樂大聲放著時，喚醒詞還抓不抓得到」——以及 duck-on-wake
的機制/體感。純軟體、零硬體，在 **Mac 本機**跑（需真麥克風+真喇叭；遠端無法測，
先備好碼、回到 Mac 前再跑）。

用法：
  # 1) 裝相依（建議獨立 venv，別污染 bot 的 venv_simon）
  python3 -m venv /tmp/owwenv && /tmp/owwenv/bin/pip install openwakeword onnxruntime sounddevice numpy
  # 2) 跑（--music 給一首大聲的歌，最能驗命門；不給則用粉紅噪音當替身）
  /tmp/owwenv/bin/python scripts/wake_over_music_poc.py --music /path/to/loud_song.mp3

觀察重點：
  - 音樂放著時喊內建英文喚醒詞（預設載入 alexa / hey jarvis / hey mycroft…）
    → 應印「🔔 WAKE ... → duck 音樂」，音樂沉 3 秒再恢復。
  - 印出的 score 就是命門指標：看音樂中喊喚醒詞的分數掉多少、過不過 0.5。
  - 靜音時 vs 音樂大聲時各喊幾次，比分數差距 = 命門有多嚴重。

⚠️ openWakeWord 只有**英文**預訓模型；這步先用英文驗「音樂中偵測 + duck」整條管線，
   中文「馬文」是之後 openwakeword.com/train 自訓的事（見 reference_physical_speaker_github_parts）。
"""
from __future__ import annotations

import argparse
import subprocess
import threading
import time

import numpy as np

MIC_RATE = 16000        # openWakeWord 要 16k mono int16
MIC_FRAME = 1280        # 80ms @ 16k（openWakeWord 建議 80ms 的倍數）
PLAY_RATE = 48000       # 喇叭播放取樣率
PLAY_BLOCK = 960        # 20ms @ 48k
DUCK_GAIN = 0.15        # 喚醒時音樂降到 15%（聽得出沉下去）
DUCK_HOLD_S = 3.0       # duck 持續秒數（模擬確認安靜窗）
WAKE_THRESHOLD = 0.5    # openWakeWord 內建模型預設閾值
LOG_SCORE = 0.20        # 分數超過就印（觀察音樂中的近偵測）


class MusicPlayer:
    """背景播放音樂（可 duck）。ffmpeg 解碼任意檔 → 48k stereo s16 loop；無檔用粉紅噪音替身。"""

    def __init__(self, path: str | None) -> None:
        self._samples = self._load(path)  # np.int16 [N, 2] 或 None（→噪音）
        self._pos = 0
        self._duck_until = 0.0
        self._stream = None

    @staticmethod
    def _load(path: str | None):
        if not path:
            print("ℹ️  未給 --music，用粉紅噪音當『音樂』替身（真命門請給大聲的歌）", flush=True)
            return None
        try:
            out = subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "quiet", "-i", path,
                 "-ac", "2", "-ar", str(PLAY_RATE), "-f", "s16le", "pipe:1"],
                capture_output=True, check=True,
            ).stdout
            arr = np.frombuffer(out, dtype=np.int16).reshape(-1, 2)
            print(f"🎵 已載入音樂：{path}（{len(arr)/PLAY_RATE:.0f}s，loop 播放）", flush=True)
            return arr
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  ffmpeg 解碼失敗（{e}），改用粉紅噪音替身", flush=True)
            return None

    def duck(self) -> None:
        self._duck_until = time.monotonic() + DUCK_HOLD_S

    def _next(self, n: int) -> np.ndarray:
        gain = DUCK_GAIN if time.monotonic() < self._duck_until else 1.0
        if self._samples is None:  # 粉紅噪音替身
            return (np.random.randn(n, 2) * 4000 * gain).astype(np.int16)
        end = self._pos + n
        if end <= len(self._samples):
            chunk = self._samples[self._pos:end]
            self._pos = end
        else:  # wrap（loop）
            chunk = np.vstack([self._samples[self._pos:], self._samples[:end - len(self._samples)]])
            self._pos = end - len(self._samples)
        return (chunk.astype(np.float32) * gain).astype(np.int16)

    def start(self) -> None:
        import sounddevice as sd  # noqa: PLC0415

        def _cb(outdata, frames, _t, _status):  # noqa: ANN001
            outdata[:] = self._next(frames)

        self._stream = sd.OutputStream(
            samplerate=PLAY_RATE, channels=2, dtype="int16",
            blocksize=PLAY_BLOCK, callback=_cb,
        )
        self._stream.start()

    def stop(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    ap = argparse.ArgumentParser(description="音樂中 openWakeWord 偵測 → duck 命門原型")
    ap.add_argument("--music", default=None, help="音樂檔（mp3/wav/…；不給用粉紅噪音替身）")
    ap.add_argument("--framework", default="onnx", choices=["onnx", "tflite"],
                    help="openWakeWord 推論框架（Mac 建議 onnx）")
    ap.add_argument("--threshold", type=float, default=WAKE_THRESHOLD)
    args = ap.parse_args()

    import openwakeword  # noqa: PLC0415
    from openwakeword.model import Model  # noqa: PLC0415

    print("⬇️  確認/下載 openWakeWord 預訓模型…", flush=True)
    try:
        openwakeword.utils.download_models()
    except Exception as e:  # noqa: BLE001
        print(f"⚠️  download_models 失敗（{e}）；若已下載可忽略", flush=True)
    model = Model(inference_framework=args.framework)  # 載入全部內建英文模型
    print(f"✅ 模型載入：{list(model.models.keys())}", flush=True)

    player = MusicPlayer(args.music)
    player.start()

    import sounddevice as sd  # noqa: PLC0415
    print("🎧 聽取中…音樂放著時喊喚醒詞（如 'alexa' / 'hey jarvis'）。Ctrl-C 結束。", flush=True)
    last_wake = 0.0
    with sd.InputStream(samplerate=MIC_RATE, channels=1, dtype="int16", blocksize=MIC_FRAME) as mic:
        try:
            while True:
                frame, _ = mic.read(MIC_FRAME)  # (1280, 1) int16
                scores = model.predict(frame.flatten())
                name, score = max(scores.items(), key=lambda kv: kv[1])
                now = time.monotonic()
                if score >= args.threshold and now - last_wake > DUCK_HOLD_S:
                    print(f"🔔 WAKE  {name}  score={score:.2f}  → duck 音樂 {DUCK_HOLD_S:.0f}s", flush=True)
                    player.duck()
                    last_wake = now
                elif score >= LOG_SCORE:
                    print(f"   …近偵測 {name} score={score:.2f}（未過 {args.threshold}）", flush=True)
        except KeyboardInterrupt:
            print("\n👋 結束。", flush=True)
    player.stop()


if __name__ == "__main__":
    main()
