# MarvinControl — macOS 選單列控制

Pi 網頁控制台（`http://<pi>:8766/`）的原生 macOS 版本。常駐右上選單列，點一下彈出面板。
macOS 的「控制中心」不開放第三方元件，選單列 App 是最接近的原生體感。

## 功能（鏡射網頁面板）

- 音量滑桿 + 靜音 + SoC 溫度
- 聲音風格：校正 / 流行 / 播客 / 空間
- 在家 / 離家（麥克風＋DigiAMP）
- PTT 開始/結束對話
- 點歌、播放控制、說一句話（走 Mac 大腦 `/say`）
- 現正播放

## 建置

```bash
./build.sh          # 需 Xcode command line tools（swiftc）、macOS 13+
open build/MarvinControl.app
```

第一次開啟後點面板右下 ⚙️ 填入 **Token**（= Mac `.env` 的 `MARVIN_TEXT_TOKEN`，三入口共用那顆；
Pi 端環境變數名為 `MARVIN_VOL_TOKEN`，值由安裝時從此替換）。填完即時儲存（`@AppStorage`，
存於 `com.marvin.control` 偏好檔），可按「套用並測試連線」立即驗。
Pi / Mac 位址預設 `100.121.35.41:8766` / `100.123.68.86:8790`，可一併修改。

> ⚠️ **明文 HTTP**：原生 App 的 `URLSession` 受 App Transport Security 管制、預設擋 `http://`。
> `build.sh` 在 Info.plist 加了 `NSAllowsArbitraryLoads` 放行（Tailscale 私網）。若哪天「瀏覽器能連、
> App 卻紅點」，先查這條有沒有掉。

## 開機自啟（LaunchAgent，比拖登入項目可靠）

建 `~/Library/LaunchAgents/com.marvin.control.plist`（絕對路徑要對到你的 `build/` 位置）：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.marvin.control</string>
  <key>ProgramArguments</key>
  <array><string>/絕對路徑/device/mac-control/build/MarvinControl.app/Contents/MacOS/MarvinControl</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
  <key>ProcessType</key><string>Interactive</string>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
</dict></plist>
```

```bash
pkill -x MarvinControl                                              # 先關手動開的實例
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.marvin.control.plist   # 載入+立即啟動
launchctl bootout   gui/$(id -u)/com.marvin.control                 # 之後要停用
```

`KeepAlive=false` 所以面板按「結束」能真的關掉；`LimitLoadToSessionType=Aqua` 確保只在圖形登入起。
plist 含本機絕對路徑，屬機器本地檔、不進 repo。**別搬走 `device/mac-control/` 或刪 `build/`**，否則路徑失效。

## 端點對照

| 面板動作 | 端點 |
|---|---|
| 音量 / 溫度 | `POST/GET {pi}/vol` |
| 聲音風格 | `POST {pi}/profile` |
| 在家離家 | `POST {pi}/presence` |
| PTT | `POST {pi}/ptt` |
| 點歌/說話/播放控制 | `POST {mac}/say` |
| 現正播放 | `GET {mac}/now` |
