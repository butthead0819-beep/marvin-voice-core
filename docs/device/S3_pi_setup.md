# S3 — Pi 3B + DigiAMP+ + XVF3800 設定 runbook（硬體到貨後）

> 全機械步驟。每節末有驗收；不過就查故障表，查不到就停下問使用者。

## 3.1 燒系統
1. Raspberry Pi Imager → **Raspberry Pi OS Lite (64-bit)**（Pi 3B 可跑 64-bit；wyoming 建議 64-bit）。
2. Imager 進階設定：hostname `marvinpi`、開 SSH、填 WiFi（2.4G）、使用者 `pi`。
3. 開機、`ssh pi@marvinpi.local`。驗收：登得進。

## 3.2 DigiAMP+（喇叭輸出）
1. **接線**：HAT 直插 40-pin GPIO；喇叭線接螺絲端子（L+/L-/R+/R-）；12-24V barrel 進 HAT。**Pi 自己的 microUSB 電源不要插**（HAT 併供 5.1V 給 Pi）。
2. `/boot/firmware/config.txt`（舊版路徑 `/boot/config.txt`）：
   ```
   dtoverlay=iqaudio-dacplus,unmute_amp
   dtparam=audio=off
   ```
   （`unmute_amp` 讓擴大機開機解除靜音；內建 audio 關掉避免搶 default 卡。）
3. `sudo reboot` → `aplay -l` 應列出 `IQaudIODAC`。
4. **驗收**：`speaker-test -c 2 -t wav -D default` 書架喇叭出聲。沒聲：查 `alsamixer` 音量、`dtoverlay` 拼字、電源瓦數（12V ≥3A）。

## 3.3 XVF3800 麥克風
1. USB 插上 → `arecord -l` 應列出 ReSpeaker/XVF 裝置。
2. **驗收**：`arecord -D plughw:<卡號>,0 -r 16000 -c 1 -f S16_LE -d 5 test.wav && aplay test.wav` 講話→喇叭回放聽得到自己。
3. 記下裝置名（如 `plughw:CARD=XVF3800,DEV=0`），下節用。

## 3.4 wyoming-satellite + wyoming-openwakeword
照官方教學（https://github.com/rhasspy/wyoming-satellite/blob/master/docs/tutorial_2mic.md ，硬體段換成我們的 ALSA 裝置名）：
```bash
sudo apt-get update && sudo apt-get install -y python3-venv git libopenblas-dev
git clone https://github.com/rhasspy/wyoming-satellite.git ~/wyoming-satellite
cd ~/wyoming-satellite && python3 -m venv .venv && .venv/bin/pip install -f 'https://synesthesiam.github.io/prebuilt-apps/' -e '.[all]'
git clone https://github.com/rhasspy/wyoming-openwakeword.git ~/wyoming-openwakeword
cd ~/wyoming-openwakeword && python3 -m venv .venv && .venv/bin/pip install -e .
# 放「馬文」模型（S1 產出）
mkdir -p ~/wakewords && scp <Mac>:.../models/wakeword/mawen_v1.tflite ~/wakewords/
```
啟動（先手動跑通，再照官方教學包 systemd service）：
```bash
# 終端 1：喚醒服務
~/wyoming-openwakeword/.venv/bin/python -m wyoming_openwakeword \
  --uri 'tcp://127.0.0.1:10400' --custom-model-dir ~/wakewords --preload-model 'mawen_v1'
# 終端 2：衛星（⚠️ mic 16k/1ch、snd 48k/2ch —— Mac 橋的 send_pcm 是 48k stereo）
~/wyoming-satellite/.venv/bin/python -m wyoming_satellite \
  --name 'marvin-satellite' --uri 'tcp://0.0.0.0:10700' \
  --mic-command 'arecord -D plughw:CARD=XVF3800,DEV=0 -r 16000 -c 1 -f S16_LE -t raw' \
  --snd-command 'aplay -D default -r 48000 -c 2 -f S16_LE -t raw' \
  --wake-uri 'tcp://127.0.0.1:10400' --wake-word-name 'mawen_v1'
```
**驗收**：
1. Pi log：喊「馬文」→ openwakeword log 出現 detection。
2. Mac：`nc -z marvinpi.local 10700 && echo OK` → OK。
3. （S4 之後）Mac 橋連上收到 Detection。

## 故障表
| 症狀 | 查 |
|---|---|
| aplay 無聲 | alsamixer 靜音/音量、config.txt overlay、電源不足重開 |
| arecord 無此裝置 | `lsusb` 看 XVF3800 在不在、USB 線/供電 |
| 喚醒不觸發 | 先用內建英文模型 `--preload-model 'hey_jarvis'` 隔離是模型問題還是管線問題 |
| Mac 連不上 10700 | Pi 防火牆（預設無）、`--uri 0.0.0.0` 沒打錯、同網段 |
