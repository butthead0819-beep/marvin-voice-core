# S4 — 整合點火 runbook（橋接腦 + 身分 + duck + 播放）

> S3 完成後做。**每小節：改 → 測（TDD）→ 全 suite 綠 → 才進下一節。**
> 橋本體已寫好測綠（`marvin_voice_core/wyoming_bridge.py`），這裡只做接線。

## 4.1 身分映射（衛星/本機 → 既有講者「狗與露」）
**落點＝`discord_voice_engine.py:879` 附近**（`speaker_name = f"User_{user_id}"` 那行之後、member 解析之前）。加：
```python
        speaker_name = f"User_{user_id}"
        # 🛰️ [Identity Map] 非 Discord 來源（衛星/本機）→ 既有講者身分＝記憶延續
        # （project_identity_unification；env 不設＝維持 User_xxx 舊行為）
        _id_map = {"satellite": os.getenv("MARVIN_SATELLITE_SPEAKER", ""),
                   "local": os.getenv("MARVIN_LOCAL_SPEAKER", "")}
        _mapped = _id_map.get(str(user_id), "")
        if _mapped:
            speaker_name = _mapped
```
`.env` 加：`MARVIN_SATELLITE_SPEAKER=狗與露`、`MARVIN_LOCAL_SPEAKER=狗與露`。
**TDD**：新測試檔 `tests/test_satellite_identity_map.py`——env 設時 user_id="satellite" → speaker=狗與露；env 不設 → `User_satellite`（舊行為）；Discord member 正常解析不受影響。

## 4.2 衛星播放裝置（mixer 泵 → 衛星喇叭）
新檔 `marvin_voice_core/wyoming_speaker_output.py`（LocalSpeakerDevice 的 `output=` 注入點本來就吃任何有 `write(frame)/close()` 的物件）：
```python
"""泵執行緒 write(48k stereo s16 frame) → event loop 送 AudioChunk 給衛星播放。"""
import asyncio, logging
logger = logging.getLogger(__name__)

class WyomingSpeakerOutput:
    def __init__(self, bridge, loop):
        self._bridge, self._loop = bridge, loop
        self._q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._task = None
        self._started = False

    def _ensure_task(self):
        if self._task is None:
            self._task = self._loop.create_task(self._drain())

    async def _drain(self):
        from wyoming.audio import AudioChunk, AudioStart
        c = self._bridge._client
        if c is None: return
        await c.write_event(AudioStart(rate=48000, width=2, channels=2).event())
        while True:
            frame = await self._q.get()
            if frame is None: break
            await c.write_event(AudioChunk(rate=48000, width=2, channels=2, audio=frame).event())

    def write(self, frame: bytes) -> None:   # 泵執行緒呼叫
        self._loop.call_soon_threadsafe(self._ensure_task)
        try: self._loop.call_soon_threadsafe(self._q.put_nowait, frame)
        except Exception: pass                # 滿了丟幀（網路塞就別堆積延遲）

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._q.put_nowait, None)
```
**TDD**：假 bridge._client 收 events → write N 幀 → drain 後 client 收到 AudioStart+N AudioChunk；close 結束 task。
⚠️ 已知取捨：持續泵＝持續送流（含靜音 ~1.5Mbps）。2.4G WiFi 可承受；若不穩，後續再做「idle 停送」優化，先求通。

## 4.3 入口：`start_satellite_listening` + `main_satellite.py`
**照抄 `cogs/voice_controller_connection.py:776` 的 `start_local_listening`**（同一模式），差異只有：
- `LocalMicSink` → `WyomingSatelliteBridge(engine.process_audio_slice, host=os.getenv("MARVIN_SATELLITE_HOST","marvinpi.local"), user_id="satellite", on_detection=..., loop=bot.loop)`
- `set_local_speaker(LocalSpeakerDevice())` → `set_local_speaker(LocalSpeakerDevice(output=WyomingSpeakerOutput(bridge, bot.loop)))`
- `create_task(sink.start())` → 重連迴圈：
  ```python
  async def _bridge_forever():
      while True:
          try: await bridge.run()
          except Exception as e: logger.warning(f"🛰️ bridge error: {e}")
          await asyncio.sleep(5)   # 衛星斷線/重啟 → 5s 後重連，不炸腦
  self.bot.loop.create_task(_bridge_forever())
  ```
- on_detection hook＝duck：`lambda name: self._mixer.duck_for_wake() if getattr(self, "_mixer", None) else None`
- consent stub、`_local_mode=True`、late-skip 120s：**照 start_local_listening 原樣**。

`main_satellite.py`＝複製 `main_local.py`，把 `vc.start_local_listening()` 換 `vc.start_satellite_listening()`。
**TDD**：mirror `tests/test_local_input_seam.py` 寫 `tests/test_satellite_input_seam.py`（bridge 綁 engine、mode 旗標、speaker device 是注入 output 的、consent stub）。

## 4.4 點火順序（在家、硬體就緒）
1. Pi：起 openwakeword + satellite（S3 的兩個指令）。
2. Mac：`venv_simon/bin/python main_satellite.py`。
3. **驗收天梯**（MASTER_PLAN 6 階）逐階打勾：連上→講話有 STT→喚醒→喇叭回話→音樂中喚醒 duck。
4. 每階失敗查表：
   | 階 | 症狀 | 查 |
   |---|---|---|
   | 3 | 橋連不上 | Pi satellite 有跑？`nc -z marvinpi.local 10700`；host env 對嗎 |
   | 4 | 有連線無 STT | Mac log 有無 `🛰️ 衛星開始串流`；沒有＝Pi 端喚醒沒觸發（先換英文模型隔離）；有串流無字＝看 `[Core_LocalSink]`/STT log |
   | 5 | 有 STT 無回話聲 | grep `⚠️ [TTS] 無可用播放裝置` / `⏭️ [TTS Load Drop]`；snd-command 格式 48k/2ch 對嗎 |
   | 6 | 音樂不 duck | `MARVIN_WAKE_DUCK` 沒被設 0；on_detection 有接 `_mixer.duck_for_wake` |

## 4.5 收尾
全通後：更新記憶 `project_marvin_physical_speaker`（S4 完成+實測結果）、把 S0 聲學觀察一併記錄。剩 S5 存在感層（見 MASTER_PLAN）。
