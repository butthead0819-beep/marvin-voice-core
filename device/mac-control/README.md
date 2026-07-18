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

第一次開啟後點面板右下 ⚙️ 填入 **Token**（= Pi 的 `MARVIN_VOL_TOKEN`）。
Pi / Mac 位址預設 `100.121.35.41:8766` / `100.123.68.86:8790`，可一併修改。設定存 UserDefaults。

開機自動啟動：把 `build/MarvinControl.app` 拖進 **系統設定 → 一般 → 登入項目**。

## 端點對照

| 面板動作 | 端點 |
|---|---|
| 音量 / 溫度 | `POST/GET {pi}/vol` |
| 聲音風格 | `POST {pi}/profile` |
| 在家離家 | `POST {pi}/presence` |
| PTT | `POST {pi}/ptt` |
| 點歌/說話/播放控制 | `POST {mac}/say` |
| 現正播放 | `GET {mac}/now` |
