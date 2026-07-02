# SPOF 清單 — 斷了全死的單點（2026-07-02 失效模式分析）

依爆炸半徑排序。每個節點附：症狀、診斷入口、現有防護。
debug「沒聲音/沒反應」時從上往下掃。

## 1. Mac mini 本體（M1 8GB）
- **斷法**：斷電、系統更新重開、磁碟滿。
- **症狀**：一切消失。
- **防護**：無（單機自架的本質）。復原=開機 → launchd 自動拉起全鏈。
- **診斷**：`launchctl list | grep marvin`。

## 2. launchd → wrapper → venv → main_discord.py 啟動鏈
- **斷法**：plist 壞、venv 被動到、.env 缺（consent.json 等 8 個 gitignored
  runtime state 檔遷移遺失＝silent killer，見 memory runtime_state_files）。
- **症狀**：bot 不在線；或起來但行為怪（state 檔缺）。
- **防護**：run_bot attempt 計數；防線① probe 的 heartbeat check。
- **診斷**：`tail bot_stdout.log` 看 `[run_bot] entered/attempt`。

## 3. bot event loop（單進程 asyncio）
- **斷法**：busy-spin（2026-06-29 實案：`while: await 早退協程()` 不讓出）、
  同步阻塞呼叫混進 loop。
- **症狀**：**進程活著但全聾啞**——launchd 不重啟、ErrorDispatcher DM 發不出。
- **防護**：防線① 心跳信標（liveness_beacon.py, 30s）+ 外部 probe 驗 staleness
  （heartbeatprobe cron, 30min）→ REST DM（不依賴 bot 進程）。
- **診斷**：`records/heartbeat.json` 的 ts；`grep 'Loop thread traceback'`。

## 4. DAVE/SRTP 雙層解密（davey）
- **斷法**：改頻道 bitrate → secret_key 沒同步（CryptoError 風暴實案）、
  davey 套件 API 漂移。
- **症狀**：**斷一層 STT 全死**、聽到的全是糊的。
- **防護**：Sentinel（零成功解密不豁免，74b78cd）+ KeySync 封包級換金鑰。
- **診斷**：`grep CryptoError bot_stdout.log`（80/min 級＝風暴）。

## 5. macos_stt_v2_bin（SpeechAnalyzer）
- **斷法**：macOS 更新改 API、模型資產損毀、bin 被誤刪/沒重編。
- **症狀**：STT 空輸出 → 降級鏈（v1→Groq）吃住一部分，全斷則聾。
- **防護**：引擎降級鏈；防線① probe 的 stt check（30min 真跑一次 bin）。
- **診斷**：`./macos_stt_v2_bin <wav>` 手跑；`grep 'STT Fatal'`。

## 6. edge-tts（微軟服務）
- **斷法**：微軟限流（晚間高峰實案，自己來自己走）、服務中斷。
- **症狀**：**有回應沒聲音**（skip 沒 ack、龍蝦沒語音）。
- **防護**：say 備援（振幅已 peak_normalize 拉滿）；防線① probe 的 tts check。
- **診斷**：`grep 'No audio was received'`。

## 7. 單 query_queue + 唯一 worker
- **斷法**：不會斷，會**堵**——worker 內等問句/cleaner 卡住，後面全陪等。
- **症狀**：p90 21s／「20 秒無反應」＝體感死亡。兩人同窗才爆。
- **防護**：問句逾時 4s 止血；根治（per-speaker 序列化）在觀察名單。
- **診斷**：pipeline_timing.jsonl 的 queue_wait 段。

## 8. LLM 免費池（Groq/Cerebras/Gemini）
- **斷法**：模型 ID 過期（Cerebras 全 404 實案）、quota 爆、429 連鎖。
- **症狀**：cleaner/回應遲鈍或 silent failure；已有付費兜底接 14% 硬失敗。
- **防護**：pool failover + 付費兜底 + 3am 報表。
- **診斷**：`grep 'LLMBus'`；記得排除 pytest 污染（6/12 前教訓）。

## 9. Discord gateway 本身
- **斷法**：Discord 全域故障、token 被 revoke。
- **症狀**：離線；REST probe DM 也會失敗（同一平台）——此時你自己會發現。
- **防護**：discord.py 自動重連。無需額外投資。

---
維護：新增「斷了全死」節點時補一節。防線①=heartbeatprobe cron、
防線②=記憶隔離、防線③=寫入檢疫（見 tests/test_memory_*）。
