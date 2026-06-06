# AGENTS.md — Marvin Discord Voice Bot

跨 coding agent 的入口檔。新接手的 agent 先讀這頁，再依指引深入。

## ⚠️ 最高優先規則

**所有回覆一律繁體中文**（台灣口語），無論問題是什麼語言。

## 讀這些（依序）

| 檔 | 內容 |
|---|---|
| **`CLAUDE.md`** | 硬性工作守則：TDD 流程、Voice Agent 設計規範（STT/VAD/IntentBus/Game 模式）、日誌規範。**等同本專案的 constitution，違反不行。** |
| **`docs/AGENT_MEMORY.md`** | 44 條從前任 agent 累積的 institutional knowledge（決策、踩雷、修正，含 why）。**讀 code 前先掃一遍**，省下重踩坑。 |
| `DESIGN.md` / `README.md` | 架構與專案總覽 |
| `TODOS.md` | 待辦 / 進行中 |
| `docs/PLAN_B_public_bot.md` | 公開 bot 計劃（冷凍待命，有客戶再開始） |

> ⚠️ `AGENT_MEMORY.md` 是某個時間點的快照；裡面的 `file:line` citation 可能已漂移，引用前先對現有 code 驗證。別信 git 日期定年（git floor=開源日 2026-05-07，但 bot 早就存在）。

## 這是什麼

多人 Discord **語音** AI 夥伴（DJ + 遊戲主持 + 毒舌人格 + 自發漫才 + 視覺）。零鍵盤：所有互動純語音。核心流水線：

```
Discord Audio Sink → VAD → STT → pre_filter → Cleaner LLM
  → IntentBus (agents bid → max wins) → winner.handler / Marvin LLM fallback
```

每層用 Protocol 解耦，每個 I/O 都要有 fallback（優雅降級）。

## 鐵則（細節見 CLAUDE.md / AGENT_MEMORY.md）

- **TDD 強制**：先寫失敗測試 → 確認全紅 → 最小實作 → 全綠 → 測試與實作同 commit。
- **LLM 一律走 bus**：`from llm_pool import ...` / `bot.router._call_llm(...)`；禁 caller 自開 client 或寫死 model ID（free tier model ID 會悄悄 deprecate 全 404）。
- **新 intent 不改 voice_controller 的 if/elif**：寫一個 `DeclarativeIntentAgent` 註冊到 IntentBus（範本見 `intent_agents/base.py` + reference agents）。`bid()` 必須 sync ≤5ms、禁 I/O、永遠回 `Bid`（未命中回 confidence=0.0）。
- **STT async 安全**：CPU-bound 跑 `asyncio.to_thread`；subprocess 用 `asyncio.create_subprocess_exec`；Sink.write 是同步執行緒用 `loop.create_task()`。
- **數據驅動**：查故障/延遲先撈原始數據定位到 code 行，禁「可能是 X」推測當結論。
- **改 daily/weekly cron 腳本後，立刻手動 e2e 跑一次**（`--speaker X --force` 之類），別等 cron 自然跑——低頻 job 的 bug 會藏好幾天。

## 跑起來

```bash
# 測試
source venv_simon/bin/activate && python3 -m pytest tests/ -q
# 單一檔
python3 -m pytest tests/test_<feature>.py -q

# Bot：launchd 管理（label com.antigravity.marvin.bot）
# 重啟：
launchctl kickstart -k gui/$(id -u)/com.antigravity.marvin.bot
# log：bot_main.log（啟動序列、IntentBus bid）
```

跑在 **M1 8GB**（記憶體吃緊，閒置就 ~2.9GB 壓縮）。STT 主力是 macOS Swift（`macos_stt_bin`，本機免費、比 Groq 快），雲端 fallback 走 Groq/Yating。

## ⚠️ Gitignored 但關鍵的 runtime state

這些檔不在 git 但 bot 靠它們運作（遷移/重建環境時忘了帶＝silent killer）。完整清單見 `AGENT_MEMORY.md` 的 `runtime_state_files` 條。重點：`consent.json`、`suki_memory.json`、`marvin.db`、各 `.env` key。

## 記憶導出

`docs/AGENT_MEMORY.md` 由 `scripts/export_agent_memory.py` 從前任 Claude Code 的 per-project 記憶導出（2026-06-06）。換回 Claude 或要更新時可重跑。新 agent 沒有那套記憶系統，請把學到的決策/踩雷直接補進 `AGENT_MEMORY.md`。
