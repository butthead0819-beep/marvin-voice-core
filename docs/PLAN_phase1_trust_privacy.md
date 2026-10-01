# 實作計劃：Phase 0／1 信任與隱私（給 Gemini 執行）

> 2026-10-01 由 Claude Code 依 `validation/REQUIREMENTS_v2.md`（含頂部「現況校正」）與程式碼現況撰寫。
> 執行者：Gemini。驗收：Claude Code（逐行看 diff、自己重跑測試、做 mutation check）。

---

## 0. 執行規則（每一條都要遵守）

1. **照本文件做，不要自己發揮設計。** 本文件跟程式碼對不上、或你覺得設計有問題 → **停下來，把問題寫在回報裡**，不要猜一個你覺得合理的版本。
2. **只改本文件列出的檔案與位置。** 不順手重構、不清無關的死碼、不改格式。
   **一律在獨立 git worktree 裡工作**（例：`git worktree add ../dvb-<分支名> -b <分支名> main`）。repo 主目錄是 prod bot 的執行目錄：**不准在主目錄切分支、stash、reset 或 checkout**（2026-10-01 Stage A/B 曾在主目錄切分支並 stash 掉使用者未 commit 的檔案）。
3. **TDD**：每一項先寫會失敗的測試，跑一次確認是紅的，再寫實作。測試必須真的 import 並呼叫被測程式碼；**不准把被測邏輯複製一份到測試檔裡驗算**。
4. async 測試用 `@pytest.mark.asyncio`，不要用 `asyncio.run`（會污染其他測試）。
5. 測試指令：`./venv_simon/bin/python -m pytest -q`（在 repo 根目錄）。不要用系統的 `python3`。全套目前基準 **6142 passed**，做完不能少於這個數、不能有 failed。
6. **不要重啟 bot、不要動 launchd、不要刪除或改寫任何資料檔**（`marvin.db`、`*.json`、`*.jsonl`、`records/`、`data/`、`.chroma_db/`、log）。需要動資料的步驟本文件都寫成「只產出 dry-run 報告」。
7. **不要 push、不要開 PR。** 每個 Stage 開一條本機分支，每一項一個 commit，commit 訊息用繁體中文、格式 `類型(範圍): 說明`。
8. `cogs/voice_controller.py` 有 AST 棘輪守門（`tests/test_voice_controller_size_budget.py`），**不准調高任何 budget**。本文件對 voice_controller.py 的修改都設計成不增加 statement 數；如果棘輪測試還是紅了，停下來回報。
9. 每個 Stage 做完，回報：改了哪些檔、每個測試的名稱、全套測試最後一行輸出。

---

## Stage A：現在就做（分支 `feat/phase1-stage-a`）

### A1. 權限：`/marvin_reboot` 只有 owner 能用（STATUS 🔴4）

> **`!sync`（STATUS 🟠8）不用改**：它寫在 `MarvinBot(commands.Bot)` 類別裡，`commands.Bot` 子類別的 `@commands.command` 方法不會自動註冊（2026-10-01 實測 `get_command("sync")` 回 None，log 也從無執行紀錄），任何人都叫不出來。**不要動它、也不要幫它註冊**；只在 A4 把 STATUS 的說法改正。

**新檔 `owner_auth.py`**（repo 根目錄）：

```python
"""營運者（owner）身分判定——全 bot 單一來源。

MARVIN_OWNER_ID 優先，其次 LOCAL_USER_ID（main_discord / voice_controller 既有用法），
都沒設就用既有預設值（與 wake_sample_collector.py / owner_song_voice_samples.py 一致）。
"""
import os

_DEFAULT_OWNER_ID = "876758076831723580"


def owner_id() -> int:
    raw = os.getenv("MARVIN_OWNER_ID") or os.getenv("LOCAL_USER_ID") or _DEFAULT_OWNER_ID
    try:
        return int(raw)
    except ValueError:
        return int(_DEFAULT_OWNER_ID)


def is_owner(user_id) -> bool:
    try:
        return int(user_id) == owner_id()
    except (TypeError, ValueError):
        return False


OWNER_ONLY_MESSAGE = "🔒 這個指令只有營運者能用。"
```

**`cogs/voice_controller.py` 的 `marvin_reboot`**（約 820 行）：只加一個 decorator，函式本體不動（decorator 不算 statement，棘輪不會動）：

```python
@app_commands.command(name="marvin_reboot", ...)   # 原樣
@app_commands.describe(...)                          # 原樣
@app_commands.check(lambda i: is_owner(i.user.id))   # ← 新增這一行
async def marvin_reboot(...):                        # 原樣
```
檔頭加 `from owner_auth import is_owner`（import 不算 statement）。

**`main_discord.py` 的 `on_app_command_error`**（約 253 行）：在 `_is_expired_interaction_error` 那段之後、`logger.error(...)` 之前，插入：

```python
if isinstance(error, app_commands.CheckFailure):
    logger.info(f"🔒 [Owner Only] {interaction.user} 嘗試 /{cmd_name}，已拒絕")
    if not interaction.response.is_done():
        try:
            await interaction.response.send_message(OWNER_ONLY_MESSAGE, ephemeral=True)
        except discord.HTTPException:
            pass
    return
```
理由：不能落到 `logger.error`——ErrorDispatcher 會把每次 ERROR 當事故 DM 給 owner。

`main_discord.py` 檔頭加 `from owner_auth import OWNER_ONLY_MESSAGE`。

**`cogs/voice_controller_connection.py` 的 `self_restart`**（約 1125–1131 行）：**刪掉**把 git pull 輸出貼到 `self.active_text_channel` 的整段 `if self.active_text_channel: try: await ...send(f"📥 git pull ...") except: pass`。`logger.info` 那行與 `pull_summary` 保留不動（重啟完成訊息只貼 commit hash，不貼 pull 輸出，那段不用改）。

**`cogs/voice_controller.py` 的 `marvin_reboot` 訊息**：不動。

**測試 `tests/test_owner_auth.py`**：
- `is_owner`：環境變數 `MARVIN_OWNER_ID=111` → `is_owner(111)` True、`is_owner(222)` False；只設 `LOCAL_USER_ID=333` → `is_owner(333)` True；兩個都沒設 → 預設值 True；`is_owner(None)`、`is_owner("abc")` → False。用 `monkeypatch.setenv/delenv`。
- `marvin_reboot` 有掛 check：取 `VoiceController.marvin_reboot.checks`，長度 ≥1；拿第一個 check 餵 `MagicMock(user=MagicMock(id=<owner>))` 回 True、非 owner 回 False。
- `on_app_command_error` 遇到 `app_commands.CheckFailure`：回 ephemeral 的 `OWNER_ONLY_MESSAGE`，而且**沒有**呼叫 `logger.error`（patch `main_discord.logger`）。若 handler 是 closure 不好直接測，停下來回報，不要為了測試改結構。
- `self_restart(pull=True)`：patch `asyncio.create_subprocess_exec` 回假 process（stdout=b"Already up to date."、returncode=0）、patch `os.execv` 與 `self.bot.close` 防真重啟 → `active_text_channel.send` **沒有**被呼叫到含 "git pull" 的字串。

### A2. 同意通知改成實際會用到的服務（STATUS 🟠9）

**`consent_manager.py`**：
- 模組 docstring 的資料清單改成與下面通知一致（拿掉 Cerebras）。
- 新增模組層函式：

```python
def consent_notice(mention: str) -> str:
    """首次進語音頻道的資料使用聲明——內容必須與 PRIVACY.md §4 一致。"""
    return (
        f"🔐 **【資料使用聲明】** {mention}\n"
        "馬文在你說話時會：\n"
        "• 你喊「馬文」的那一句語音、以及馬文沒聽懂需要重新判斷的語音，會送到 **Google Gemini**\n"
        "• 轉成的文字連同對話記憶，會送到這些 AI 服務產生回應：**Google Gemini、Groq、Mistral、SambaNova、Together AI、OpenRouter**\n"
        "• 存在營運者電腦上的記憶資料（個人化記憶）\n\n"
        "請確認是否同意。若不同意，馬文不會處理你的語音。\n"
        "同意後可隨時用 `/marvin_optout` 撤回。"
    )
```

**`cogs/voice_controller.py`**（約 916–924 行）：把 `notice = ( f"🔐 ... " )` 整個多行字串換成 `notice = consent_notice(member.mention)`（同一個 assign statement，棘輪不變）；檔頭 import `consent_notice`。

**測試 `tests/test_consent_notice.py`**：
- 回傳字串含 mention；含 Gemini、Groq、Mistral、SambaNova、Together AI、OpenRouter 每一個；**不含** "Cerebras"、"suki_memory"。
- 對 PRIVACY.md §4 的一致性：讀 `PRIVACY.md`，切出 `## 4.` 到 `## 5.` 之間的文字，斷言上面六個服務名稱都出現在那段裡（防止兩邊之後各改各的）。

### A3. 語音進出紀錄只記 Marvin 所在頻道、只記有同意的人（STATUS 🔴6）

**`presence_logger.py`**：
1. 新增純函式（放在 `log_voice_state_change` 之前）：

```python
def presence_event(*, is_bot: bool, before_ch, after_ch, marvin_ch, consented: bool):
    """回 (event, channel) 或 None（不記）。只看相對於 Marvin 所在頻道的進出：
    進入 marvin_ch → ("join", marvin_ch)；離開 marvin_ch → ("leave", marvin_ch)。
    其他頻道之間移動、Marvin 不在語音、bot、未同意者 → None。不再產生 "move"。"""
```
   規則依序：`is_bot` → None；`marvin_ch is None` → None；`not consented` → None；`before_ch == after_ch` → None；`after_ch == marvin_ch` → join；`before_ch == marvin_ch` → leave；其他 → None。
2. `log_voice_state_change(member, before, after)` 改簽名為 `log_voice_state_change(member, before, after, *, marvin_ch, consented: bool)`；內部改用 `presence_event(...)` 決定要不要寫、寫哪個 event 與 channel。record 的欄位格式不變（`channel_id`、`channel_name` 用回傳的 channel；`is_bot` 寫 False）。
3. 更新模組 docstring：說明只記 Marvin 所在頻道、只記已同意者、沒有 move。

**`main_discord.py`**（約 350–352 行）`_on_voice_state_update_for_temp`：

```python
_vc = _member.guild.voice_client
_marvin_ch = _vc.channel if _vc else None
_vcog = self.get_cog("VoiceController")
_consented = bool(_vcog and _vcog.consent.is_consented(_member.display_name))
_log_presence(_member, before, after, marvin_ch=_marvin_ch, consented=_consented)
```
（同意目前還是用顯示名稱比對，1g 才改帳號 ID；這裡跟 consent_manager 一致即可。）

**測試 `tests/test_presence_logger_consent.py`**（只測 `presence_event` 純函式 + 一個 `log_voice_state_change` 寫檔整合測試，寫檔路徑用 `monkeypatch.setattr(presence_logger, "_LOG_PATH", tmp_path / "p.jsonl")`）：
- 未同意者進 Marvin 頻道 → None。
- 已同意者進 Marvin 頻道 → ("join", marvin_ch)；離開 → ("leave", marvin_ch)。
- 已同意者在兩個非 Marvin 頻道之間移動 → None。
- 已同意者從其他頻道移進 Marvin 頻道 → ("join", marvin_ch)；從 Marvin 頻道移到其他頻道 → ("leave", marvin_ch)，channel 是 marvin_ch 不是另一個頻道。
- Marvin 不在語音（marvin_ch=None）→ None。
- bot → None。
- 整合：未同意者事件不寫檔（檔案不存在或 0 行）；已同意者 join 寫一行，`channel_id` 等於 marvin_ch.id。

確認 `scripts/phase1_analyze_baseline.py`、`scripts/backfill_departure_cues.py` 不會因為不再有 "move" 而壞掉（只讀檔、不用改；如果需要改，停下來回報）。

**舊資料不動。** 另寫 `scripts/prune_presence_log.py`：預設 dry-run，讀 `data/voice_presence.jsonl` + `consent.json`，印出「總行數／bot 行數／未同意者行數／move 行數／會保留的行數」，**沒有** `--apply` 以外的寫入路徑；`--apply` 先把原檔複製成 `data/voice_presence.jsonl.bak_<YYYYMMDD>` 再改寫。**你只跑 dry-run，把輸出貼在回報裡，不要跑 `--apply`。** 測試用 tmp 檔驗證 dry-run 不改檔、`--apply` 會產生 .bak 並只留已同意且非 bot 的 join/leave。

### A4. 文件（Phase 0 中已經能確定的部分）

只改下列內容，其他段落不動：
1. **`/marvin_talk` 已刪除**（程式碼 9/29 刪、Discord 端已查不到）：
   - `STATUS.md` 第 78 行那列：狀態改成「已移除（2026-09-29 刪程式碼，2026-10-01 確認 Discord 已無此指令）」。
   - `PRIVACY.md` 第 31 行那個項目整條刪掉。
   - `VALIDATION.md` 第 96 行：把 `/marvin_talk` 那部分拿掉，只留 `/marvin_reboot`，並改成「`/marvin_reboot` 已限 owner」。
2. **STATUS.md「已知 bug」表**：🔴4、🟠9、🔴6 三列的狀態標成已修（寫上 commit 或分支名）；🟠8 改成「不成立：`!sync` 寫在 Bot 子類別裡沒有被註冊，叫不出來（2026-10-01 實測）；死碼保留」。
3. **PRIVACY.md**：§2 同意機制補一句「同意通知內容見 `consent_manager.consent_notice`」；§3 關於 `voice_presence.jsonl` 的「程式設定」欄改成「只記 Marvin 所在頻道、已同意者的進出」，「目前實況」欄寫「2026-10-01 前的舊紀錄含未同意者與其他頻道，待營運者確認後清理」。
4. **README.md**：
   - 開頭定位改成「遠距朋友的虛擬客廳」：散在各地的朋友晚上在 Discord 語音頻道聊天陪伴，Marvin 是客廳裡嘴很壞的室友（厭世人設保留）。只改開頭那一段，不要重寫「他能做什麼」。
   - 加一段「資料說明」：你喊「馬文」的那一句、以及沒聽懂需要重新判斷的語音會送 Google Gemini；其餘只送文字（服務清單同 PRIVACY.md §4），詳見 PRIVACY.md。
   - 「怎麼邀請」整段改成：「目前邀請制，請聯絡營運者。」
5. **VALIDATION.md**：§1 問題改成「有沒有朋友小群願意把 Marvin 當客廳，而且願意分攤成本？」；§2 門檻整段換成 `validation/REQUIREMENTS_v2.md` §5 的內容（直接引用並註明出處）；§4 步驟 2 改成「主要管道為現有成員轉介他們的其他群，不必等第 3 週；公開招募群只當觀察對象」；§8 改成「見 `validation/REQUIREMENTS_v2.md` Phase 1／2 與 `docs/PLAN_phase1_trust_privacy.md`」。
6. 不寫任何日期承諾或價格。

---

## Stage B：調查與 dry-run，做完就停（分支 `feat/phase1-stage-b`）

這個 Stage **不刪任何資料、不啟用任何排程**，只產出報告與工具，等 Jack 決定。

### B1. 資料清理排程為什麼停了（STATUS 🔴5）

事實：`~/Library/LaunchAgents/com.antigravity.marvin.feedbackbatch.plist.disabled`，每天 03:00 跑 `~/Library/Application Support/Marvin/run_feedback_batch.py`（不在 repo 裡），裡面包含 `scripts/scrub_improvement_raw.py`（judge/gaps/rescue 超過 14 天原文轉雜湊）與 transcripts 超過 14 天的 prune。最後一次執行 2026-07-09（`~/Library/Logs/Marvin/feedback_batch_cron.log`）。

要做的：
1. 讀 `feedback_batch_cron.log` 最後 200 行、`run_feedback_batch.py` 全文、`git log --all -S "feedbackbatch"` 與 `git log -- scripts/scrub_improvement_raw.py`，找出停用的原因。**找不到明確原因就寫「找不到」，不要推測。**
2. 列出 `run_feedback_batch.py` 裡每一個步驟：做什麼、會不會刪/改資料、會不會呼叫付費 API。
3. 對每個會刪資料的步驟，確認腳本有沒有 dry-run 模式；有就跑 dry-run，把「會刪幾筆、最舊的一筆日期」貼進報告；沒有就寫「沒有 dry-run，未執行」。**不要加 dry-run 參數以外的改動。**
4. 把結果寫成 `docs/REPORT_feedbackbatch_2026-10.md`。

### B2. `bot_stdout.log` 輪替提案（STATUS 🟡12）

只寫提案，不實作：`~/Library/Logs/Marvin/bot_stdout.log` 目前約 227MB、含逐字稿，由 launchd `StandardOutPath` 直接寫。提案要回答：launchd 開檔是否為 append 模式（決定 copytruncate 是否安全）、要用 newsyslog（需 sudo）還是 03:00 排程裡做 copytruncate、保留幾份、保留天數建議（對齊 14 天）。寫進同一份 REPORT 的獨立一節。

### B3. 沒有刪除規則的資料：保留期限建議

對下列每一項，寫出：存在哪、目前大小/筆數、誰在讀它（grep 程式碼列出讀取點）、建議保留期限與理由：`marvin.db` 的 `speaker_topic_graph`、`session_summaries`、`tasks`；`.chroma_db/`；`records/daily/`；`data/voice_presence.jsonl`；`records/rescue_wav/`。**只寫建議，由 Jack 決定。** 寫進同一份 REPORT。

### B4. 付費成本報表（1h 第一步，唯讀）

現況：`records/llm_paid_usage.jsonl` 每行 `{"ts", "caller", "model", "tokens", "est_usd", "in_tokens", "out_tokens"}`，**沒有 guild_id**；目前 bot 只在主群運作，所以帳本總和＝主群成本。

新增 `scripts/paid_cost_report.py`：
- 讀帳本，輸出：每月總 `est_usd`、呼叫數、tokens；當月每天的 `est_usd`；當月依 `caller` 排序的前 10 名。
- 參數：`--month YYYY-MM`（預設當月，用 Asia/Taipei 時區切日）、`--path`（預設帳本路徑）。
- 只讀不寫。
- 測試 `tests/test_paid_cost_report.py`：用 tmp 帳本（3 筆跨兩天兩個 caller、1 筆上個月）驗證月總和、日分組、caller 排序、跨月排除、時區切日（一筆 UTC 16:30 的資料要算到台北隔天）。

**guild_id 維度不在這個 Stage 做**：付費呼叫深在 LLM bus 裡，拿不到是哪個伺服器觸發的，正確做法要另外設計（例如在意圖派發入口設 contextvar）。等 Phase 2 前由 Claude Code 出設計。

---

## Stage C：等 Jack 決定之後才開工（本文件不給實作細節）

| 項目 | 卡在哪個決定 | 決定後由誰出細部設計 |
|---|---|---|
| 1c 啟用清理排程 + 補跑 | Jack 看過 B1 報告、同意備份方式後才能補跑（刪除不可逆） | Claude Code |
| 1c 其他資料的保留期限 | Jack 看過 B3 建議後決定 | Claude Code |
| 1c log 輪替 | Jack 選 B2 方案 | Claude Code |
| A3 舊進出紀錄清理 | Jack 看過 `prune_presence_log.py` dry-run 結果後決定要不要 `--apply` | 直接執行 |
| 1f 使用者自刪指令 | 指令名稱；要不要也刪「日記裡提到我的部分」（REQUIREMENTS_v2 §9.2） | Claude Code |
| 1g 身分改用帳號 ID | 較大，牽涉遷移；要先有備份與副本演練流程，Jack 看對照結果再上 prod | Claude Code |
| 1h guild_id 維度 | Phase 2 前再做 | Claude Code |

1f 建議在 1g 之後做：現在資料以顯示名稱為鍵，改過名的人刪不乾淨；先換成帳號 ID，刪除指令才可靠。

---

## 驗收清單（Claude Code 收件時逐項檢查）

- [ ] 每個新測試在實作前跑過是紅的（回報裡要附紅燈輸出）。
- [ ] 測試沒有複製被測邏輯；mutation check：把 `is_owner` 改成恆 True、把 `presence_event` 的 consented 判斷拿掉、把 `consent_notice` 拿掉 OpenRouter → 對應測試都要紅。
- [ ] `git diff` 只動本文件列出的檔案與位置。
- [ ] voice_controller 棘輪測試綠，budget 數字沒被改。
- [ ] 全套 ≥ 6142 passed、0 failed（Claude Code 自己重跑）。
- [ ] 沒有任何資料檔、launchd 檔被改動（`git status` 與 `ls -la ~/Library/LaunchAgents` 對照）。
- [ ] 文件內容與程式碼一致、沒有日期或價格承諾。
