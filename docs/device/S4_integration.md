# S4 — 整合點火 runbook（橋接腦 + 身分 + duck + 播放）

> S3 完成後做。**code 已全部寫好+測綠+land 進 main（commit b2eda60），你不用再 author。**
> 你的工作＝設 `.env` → 跑 `main_satellite.py` → 走驗收天梯。卡住查故障表，查不到停下問使用者。

## ⚠️ 給接手者：code 已鋪好，別重寫
S4 的三塊 Mac 側 code 已由強模型窗口預先寫好、TDD 測綠、land 進 main：

| 塊 | 檔案 | 測試 |
|---|---|---|
| 4.1 身分映射 | `discord_voice_engine.py::_resolve_speaker_name`（879/1071/1196 三處共用） | `tests/test_satellite_identity_map.py`（6 測） |
| 4.2 衛星播放 adapter | `marvin_voice_core/wyoming_speaker_output.py` | `tests/test_wyoming_speaker_output.py`（3 測） |
| 4.3 入口+接線 | `cogs/voice_controller_connection.py::start_satellite_listening` + `main_satellite.py` | `tests/test_satellite_input_seam.py`（10 測）+ `tests/test_main_satellite.py`（5 測） |

**先驗這些測試還綠**（環境無誤）：
```bash
cd /Users/jackhuang/Code/dvb-phase1
/Users/jackhuang/Code/Discord-voice-bot/venv_simon/bin/python -m pytest \
  tests/test_satellite_identity_map.py tests/test_wyoming_speaker_output.py \
  tests/test_satellite_input_seam.py tests/test_main_satellite.py tests/test_wyoming_bridge.py -q
```
全綠＝code 就緒，直接跳到「4.4 點火順序」。**不要**重新 author 上面任何檔案。

## 4.1 身分映射（已完成）——只需設 `.env`
非 Discord 來源（衛星 user_id="satellite"）→ 既有講者身分＝記憶延續。`.env` 加：
```
MARVIN_SATELLITE_SPEAKER=狗與露
MARVIN_LOCAL_SPEAKER=狗與露
```
不設＝維持 `User_satellite`（不亂認人）。Discord 路徑完全不受影響。
> 設計註記：映射抽在 `_resolve_speaker_name` helper、三處解析點（wake stream / speech start
> / **_flush_audio_to_stt 主 STT 路徑**）共用。舊 runbook 曾只補 wake stream 那點——那會漏掉
> 衛星音訊的主路徑、記憶仍斷；helper 版已修此洞，勿回退成單點 patch。

## 4.2 衛星播放 adapter（已完成）
`WyomingSpeakerOutput`：mixer 泵 write(48k stereo) → 經 event loop 送 AudioChunk 給衛星喇叭。
已由 `start_satellite_listening` 自動注入（`LocalSpeakerDevice(output=WyomingSpeakerOutput(...))`），
你不需手動接。
> 已知取捨（先求通）：持續泵＝持續送流含靜音（~1.5Mbps，2.4G 可承受）；`_drain` 啟動快取
> `_client` 一次，衛星斷線重連後可能掉音——遇到就重跑 `main_satellite.py`，idle 停送優化留後。

## 4.3 入口+接線（已完成）
`start_satellite_listening` 已 mirror `start_local_listening`：mic＝`WyomingSatelliteBridge`、
喇叭＝`WyomingSpeakerOutput`、喚醒 `Detection`→`_on_satellite_wake`→duck（尊重 `MARVIN_WAKE_DUCK`）、
斷線 5s 自動重連、`engine.sink=bridge.sink`（Sentinel 心跳監控）。`main_satellite.py` 是入口。
你只要跑它。

## 4.4 點火順序（在家、硬體就緒）
1. Pi：起 openwakeword + satellite（S3 的兩個指令）。
2. Mac：`.env` 設好 `MARVIN_SATELLITE_HOST=marvinpi.local`（或 Pi 的 IP）+ 上面身分 env。
3. Mac：`/Users/jackhuang/Code/Discord-voice-bot/venv_simon/bin/python main_satellite.py`。
4. **驗收天梯**（MASTER_PLAN 6 階）逐階打勾：連上→講話有 STT→喚醒→喇叭回話→音樂中喚醒 duck。
5. 每階失敗查表：
   | 階 | 症狀 | 查 |
   |---|---|---|
   | 3 | 橋連不上 | Pi satellite 有跑？`nc -z marvinpi.local 10700`；`MARVIN_SATELLITE_HOST` env 對嗎 |
   | 4 | 有連線無 STT | Mac log 有無 `🛰️ 衛星開始串流`；沒有＝Pi 端喚醒沒觸發（先換英文模型隔離）；有串流無字＝看 `[Core_LocalSink]`/STT log |
   | 5 | 有 STT 無回話聲 | grep `⚠️ [TTS] 無可用播放裝置` / `⏭️ [TTS Load Drop]`；Pi snd-command 格式 48k/2ch 對嗎 |
   | 6 | 音樂不 duck | `.env` 的 `MARVIN_WAKE_DUCK` 沒被設 0；Pi 喚醒有送 Detection（看 Mac log `🛰️ 衛星喚醒候選`） |

## 4.5 收尾
全通後：更新記憶 `project_marvin_physical_speaker`（S4 完成+實測結果）、把 S0 聲學觀察一併記錄。剩 S5 存在感層（見 MASTER_PLAN）。
