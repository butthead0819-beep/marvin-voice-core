# Marvin Device（實體音箱）Pi 設定快照

麥克風移除後，Marvin device 是「純輸出音箱」：**腦跑在 Mac**（`main_satellite.py`，不登入 Discord），
**喇叭在 Pi**（DigiAMP+ 書架喇叭）。改用文字/Siri/網頁下指令。這裡是 Pi 端設定的版控快照，
實機檔案位置見下。

## 拓撲

```
iPhone(Siri/瀏覽器) ──Tailscale──┬─► Mac 大腦 :8790  /say(下指令)  /now(現正播放)
                                 └─► Pi   :8766  /vol(音量) + GET /(控制台網頁)
Mac 大腦 ──WyomingBridge:10700──► Pi wyoming-satellite ──aplay──► DigiAMP+ ──► 喇叭
```

- 三入口共用同一個 token（實機用真值，本 repo 用 `__MARVIN_TEXT_TOKEN__` 佔位；實機從 env/systemd 注入）。
- Tailscale IP：Mac=100.123.68.86、Pi=100.121.35.41。

## 檔案與實機位置

| repo 檔 | 實機位置 | 用途 |
|---|---|---|
| `volume_server.py` | `/home/pi/marvin-device/` | 音量 HTTP 服務(:8766 `/vol`) + 控制台網頁(`GET /`)。常駐、不依賴大腦 |
| `satellite.env` | `/home/pi/marvin-device/` | wyoming-satellite 音訊裝置路由（`SND_DEVICE=plughw:CARD=IQaudIODAC,DEV=0`，用 **name** 因卡號會漂移） |
| `temp_guard.sh` | `/usr/local/bin/` | SoC 溫度防護：過熱→POST 停止播放給大腦（見下方限制） |
| `voltmon.sh` | `/usr/local/bin/` | fsync 電壓哨兵：抓 under-voltage 旗標（寫 `/var/log/voltmon.log`，每次開機記一筆） |
| `systemd/marvin-volume.service` | `/etc/systemd/system/` | volume_server 常駐 |
| `systemd/marvin-temp-guard.service` | `/etc/systemd/system/` | temp_guard 常駐 |
| `systemd/voltmon.service` | `/etc/systemd/system/` | voltmon 常駐 |
| `systemd/wyoming-satellite.service.d/override.conf` | `/etc/systemd/system/wyoming-satellite.service.d/` | 麥克風移除後：mic-command 換節流靜音源（`/dev/zero`）避免 arecord crash-loop |

## 啟動大腦（Mac，要下指令/點歌時）

```bash
cd ~/Code/Discord-voice-bot && ./venv_simon/bin/python main_satellite.py
# 先確認 24/7 Discord bot 沒開（device 直讀正本記憶，一次一具身體）
```

## ⚡ 已知硬體問題：播放中反覆斷電重開

**根因**：Pi3B + DigiAMP+ 結構＝24V → HAT 板載降壓 → 5V 餵 Pi，**同條 24V 也推 Class-D 擴大機**。
TAS5756M 晶片大音量久播 **過熱 → 電流變不穩 → 拉垮邊際的 24V/3A → 連帶 Pi 的 5V 垮 → 全燈熄、斷電重開**。

排查證據：全燈熄=真斷電（非軟體/看門狗）、voltmon 零低電壓（瞬間硬斷）、**小風扇對板吹→50 分穩**（之前 32 分）。
排除了：SD 卡、改音量 amixer、共用延長線、SoC 溫度過高、watchdog。

**解法**：
1. **給 TAS5756M 貼散熱片**（風扇已證有效，可能是主角）
2. 24V/**5A**+ 好品牌供應器（5A 純加裕度無副作用；電壓維持 24V 別超）
3. 24V 輸入端並大電解電容吸突波

## ⚠️ 溫度防護的限制

Pi 只讀得到 **SoC(cpu-thermal)** 溫度（`vcgencmd measure_temp`），**讀不到 TAS5756M 擴大機晶片**溫度——
而過熱的是那顆晶片。SoC 溫度只是弱代理（同疊板熱耦合會跟著升但幅度小、遲滯）。所以 `temp_guard`
是**安全網不是根治**，門檻（`MARVIN_TEMP_STOP_C`，預設 65°C）請依實測調整。根治仍是散熱片。

## 診斷指令（下次再跳時）

```bash
ssh pi@100.121.35.41 'sudo cat /var/log/voltmon.log; sudo cat /var/log/temp_guard.log; \
  grep 啟動 /var/log/voltmon.log; sudo journalctl -b -1 -n 30'
```
