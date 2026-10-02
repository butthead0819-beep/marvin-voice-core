# 實作計劃：舊事追問併進 DJ 串場 + 日記停止貼文（給 Gemini 執行）

> 2026-10-02 由 Claude Code 撰寫，Jack 已拍板方向。執行者：Gemini。驗收：Claude Code。

## 背景（已查證的事實）

- 「主動提起舊事」（`intent_agents/memory_callback_agent.py`）9/17 起在 SpeakBus 勝出 168 次，真的講出來 11 次（`records/speak_outcomes.jsonl` 的 `tts_pushed`）。
  - 主因：`tts_speak_policy.py` 的主動類規則 `on_stream=Verdict.DROP`——音樂播放中一律丟掉，而房間幾乎都開著自動選歌（log：`TTS Drop] PROACTIVE_TOPIC/stream` 92 次、`hot_chat` 33 次）。
  - 小 bug：handler 呼叫 `self._ctrl.speak(line, proactive=True, allow_dual=False)` 沒傳 `kind=`，被相容墊片當成 `PROACTIVE_TOPIC`，沒用到為它設計的 `SpeakKind.MEMORY_CALLBACK`（`_PROACTIVE_PATIENT`：熱聊時 DEFER 而不是 DROP）。
  - 內容品質：舊事來源是 `session_summarizer.py` 摘要 LLM 抽出的 inbound 承諾（`commitment_to_callback` → `cogs/voice_controller.py` 的 `_on_commitment_detected` → `suki_memory.enqueue_callback`）。目前 queue 裡 28 筆約 17 筆是遊戲內操作（摸狼豬、製作繃帶、收集螞蟻卵），不適合拿來追問。
- 日記：`cogs/voice_controller_system_loops.py` 的 slow loop 每 10 分鐘把摘要貼到 #馬文的厭世日記，但這個頻道也是音樂卡頻道，日記被埋掉沒人看。DJ 串場讀的是 `records/chat_summary_log.txt`（貼文前就寫好），不讀 Discord 貼文。

## Jack 的決定

1. 舊事追問**併進 DJ 串場**：音樂中不硬插話，改成讓下一段 DJ 串場有機會講「X 之前說要 Y，後來呢？」；沒音樂時維持原本的主動發話。
2. 加內容過濾：只追問現實生活的事，遊戲內操作不收。
3. 日記**停止貼到 Discord**，背後照常產生（`chat_summary_log.txt` 照寫，DJ 照用）。

---

## 0. 執行規則（每一條都要遵守）

1. 照本文件做，不要自己發揮設計。本文件跟程式碼對不上、或你覺得設計有問題 → **停下來寫在回報裡**，不要猜。
2. 只改本文件列出的檔案與位置。不順手重構、不清無關死碼、不改格式。
3. **一律在獨立 git worktree 工作**：`git worktree add ../dvb-callback-dj -b feat/callback-into-dj main`。repo 主目錄是 prod bot 的執行目錄，**不准在主目錄切分支、stash、reset、checkout**。
4. TDD：每一項先寫會失敗的測試、跑一次確認紅，再寫實作。測試必須真的呼叫被測程式碼，不准把被測邏輯複製進測試檔。
5. async 測試用 `@pytest.mark.asyncio`，不要用 `asyncio.run`。
6. 測試指令：`/Users/jackhuang/Code/Discord-voice-bot/venv_simon/bin/python -m pytest -q`（在 worktree 根目錄跑）。全套基準 **6167 passed**，不能少、不能有 failed。
7. `cogs/voice_controller.py` 有 AST 棘輪守門，**不准調高任何 budget**。本計劃對它的改動只有改一個函式呼叫的參數（不增加 statement）。
8. 不要重啟 bot、不要動 launchd、不要改任何資料檔（`suki_memory.json` 等）、不要 push。每一項一個 commit，訊息繁體中文、格式 `類型(範圍): 說明`。
9. 回報：改了哪些檔、每個測試名稱、全套測試最後一行。

---

## E1. 日記停止貼到 Discord

`cogs/voice_controller_system_loops.py` slow loop（約 218–252 行）：
- **保留** `await asyncio.to_thread(_write_rag_log, summary)`。
- **刪掉**「5. 發送到專屬頻道」整段：找 #馬文的厭世日記／marvin-diary、找不到就 `create_text_channel` 建頻道、`await target.send(f"📓 **【馬文的厭世日記】** ...")`。
- `self.pending_intervention` 的清理邏輯要保留（刪暫存音檔、設成 None），但**不再**把它附加到 summary 文字裡（因為不貼文了）。也就是：
  ```python
  await asyncio.to_thread(_write_rag_log, summary)
  # 日記不再貼 Discord（2026-10-02 Jack 定）：頻道跟音樂卡共用、被埋掉沒人看；
  # chat_summary_log.txt 照寫，DJ 串場的生活素材照用。
  if self.pending_intervention:
      old_path = self.pending_intervention.get("file_path")
      if old_path and os.path.exists(old_path):
          try: os.remove(old_path)
          except: pass
      self.pending_intervention = None
  ```
- `_find_ops_channel`（看門狗用）**不要動**。
- 測試：slow loop 產生 summary 時 → `chat_summary_log.txt` 有寫入、`active_text_channel.send` 與任何頻道的 `send` **沒有**被呼叫到含「厭世日記】」的內容、`guild.create_text_channel` 沒被呼叫。參考 `tests/test_slow_loop_skip_fallback_record.py` 的 fixture 寫法。既有測試若斷言日記有貼文，改成斷言沒貼（只改那個斷言，並在回報列出）。

## E2. 源頭過濾：只收現實生活的承諾

1. `session_summarizer.py` 的 `_SYSTEM_PROMPT` commitments 物件加一個欄位（放在 `"due_date"` 前一行）：
   ```
   "real_life": "true 或 false：現實生活中要做的事（家事、購物、聯絡家人、出門、工作）填 true；遊戲裡的操作或任務（探索、打怪、製作裝備、收集道具、整理背包）填 false",
   ```
2. `recall_handler.py` 的 `PendingConfirmation` dataclass 最後加欄位 `real_life: bool = False`。
3. `session_summarizer.py` 建 `PendingConfirmation(...)` 的地方（約 171 行）傳 `real_life=c.get("real_life") in (True, "true", "True")`。
4. `commitment_to_callback(conf)`：在現有檢查之後加 `if not getattr(conf, "real_life", False): return None`。docstring 補一句「只收現實生活的事；遊戲內操作不追問（2026-10-02）」。
5. `suki_memory.enqueue_callback` 加參數 `life: bool = False`，存進 item：`{"text": ..., "shareable": ..., "life": bool(life), "ts": ...}`。
6. `cogs/voice_controller.py` `_on_commitment_detected` 的 `enqueue_callback(speaker, text, shareable=True)` 改成 `enqueue_callback(speaker, text, shareable=True, life=True)`（只改參數）。
- 舊的 28 筆沒有 `life` 欄位 → 下面 E3／E4 都只取 `item.get("life") is True` 的，舊資料自然不會被講，7 天 TTL 後自己過期。**不要寫腳本清 `suki_memory.json`。**
- 測試：`commitment_to_callback` 對 `real_life=False`／缺欄位回 None、`True` 回 (speaker, text)；summarizer 解析 LLM JSON 時 `"real_life": true`／`"true"` → True、`false`／缺 → False；`enqueue_callback(..., life=True)` 存進的 item 有 `"life": True`。

## E3. 舊事追問進 DJ 串場的扭蛋池

1. `dj_topic_selector.py`：
   - `MODE_WEIGHTS` 加 `"callback": 1.0`。
   - `select_mode` 加 keyword 參數 `callbacks: list[str] | None = None`；在 news 之後：`cb_hit = _first_cool(callbacks, store)`；有就 `material["callback"] = cb_hit`。
   - docstring 裡「topic_text 只有 mode in {...} 才非 None」那句把 `'callback'` 加進集合。
2. `dj_narration_orchestrator.py` 的 `select_narration_mode` 加 keyword 參數 `callbacks=None`，原樣傳給 `select_mode(callbacks=callbacks)`。
3. `cogs/music_cog_dj_lyrics.py` 在 `select_narration_mode(...)` 呼叫之前（約 683 行之前，`news_items` 那段之後）：
   ```python
   callback_lines, callback_src = self._present_callbacks(present_members)
   ```
   新方法 `_present_callbacks(self, present_members) -> tuple[list[str], dict[str, tuple[str, dict]]]`：
   - `present_members` 為空或 None → `([], {})`（隱私：只講在場者自己的事）。
   - 對每個在場成員（排序後迭代，確保順序穩定）：`items = self.bot.router.memory.peek_all_shareable_callbacks(m)`，只留 `item.get("life") is True` 的，每人最多取最舊 1 筆。
   - 每筆組成字串 `f"{m} 之前說要{item['text']}"`，`callback_src[字串] = (m, item)`。
   - 任何例外 → log warning 回 `([], {})`（DJ 不能因此掛掉）。
   - `select_narration_mode(..., callbacks=callback_lines)`。
4. 同檔 `if mode == "memory_match": ... elif mode == "life": ...` 的鏈裡加一支（放在 `"interest"` 之後）：
   ```python
   elif mode == "callback":
       ctx.append(f"【你熟悉他的生活】他之前說過要做的事：\n・{topic}")
       ctx.append("開場鉤子：點名順口關心這件事後來怎麼樣了，像老朋友隨口問一句；只能講素材裡寫的事，不准自己補細節、不准替他回答。")
       _cb = callback_src.get(topic)
       if _cb:
           try:
               self.bot.router.memory.consume_callback(_cb[0], _cb[1])
           except Exception as e:
               logger.warning(f"⚠️ [DJ Callback] consume 失敗: {e}")
   ```
   （抽中就 consume：DJ 稿之後才渲染，渲染失敗就算了，不重投；`TopicCooldownStore.mark_used` 也會擋重複。）
- 測試：
  - `select_mode` 給 `callbacks=["A 之前說要買叉子"]`、其他素材都空 + 固定 rng → 可抽到 `("A 之前說要買叉子", "callback")`，且 `store.mark_used` 被呼叫；callbacks 冷卻中時不進池。
  - `_present_callbacks`：只回在場者、只回 `life is True`、每人最多 1 筆、`present_members` 空 → 空；`peek_all_shareable_callbacks` 拋例外 → 空且不拋。
  - mode=="callback" 時 ctx 含素材行、`consume_callback` 被以 (成員, item) 呼叫一次。若這支大方法不好直接測，停下來回報，**不要為了測試拆方法**。

## E4. 沒音樂時的主動追問：同樣過濾 + 補上正確類型

`intent_agents/memory_callback_agent.py`：
1. 取 `peek_all_shareable_callbacks(...)` 的結果後，只留 `item.get("life") is True` 的（找到該處、在進入主題比對之前過濾）。
2. handler 的 `await self._ctrl.speak(line, proactive=True, allow_dual=False)` 加上 `kind=SpeakKind.MEMORY_CALLBACK`（從 `tts_speak_policy` import `SpeakKind`）。
- `tts_speak_policy.py` **不要改**（音樂中照樣 DROP，音樂中的出口是 E3 的 DJ 串場）。
- 測試：沒有 `life` 的 item 不會讓 agent 出價（回 None 或 dense 0.0，照該 agent 現有的「沒素材」行為）；handler 呼叫 `speak` 時 `kind=SpeakKind.MEMORY_CALLBACK`。

## E5. 文件

- `PRIVACY.md` 提到 Discord 日記的四處都要改（行號為 2026-10-02 版本）：
  - 第 58 行「日記」列：存在哪改成只有 `records/chat_summary_log.txt`，並註明「2026-10-02 起不再貼到 Discord；舊的 Discord 貼文依該伺服器管理」。
  - 第 73 行「…以及貼到 Discord 的日記」：拿掉日記這一項。
  - 第 89 行 Discord 那列「回覆、日記、同意通知」：拿掉「日記」。
  - 第 109 行「Discord 上的日記訊息」：保留（舊貼文還在），後面補「（2026-10-02 起不再新增）」。
- 不寫日期承諾。

---

## 驗收清單（Claude Code 收件時檢查）

- [ ] 主目錄沒被切分支／stash（`git -C /Users/jackhuang/Code/Discord-voice-bot branch --show-current` = main）。
- [ ] 新測試實作前是紅的（回報附紅燈輸出）。
- [ ] mutation：拿掉 `life is True` 過濾、拿掉 `real_life` 檢查、拿掉 `MODE_WEIGHTS["callback"]`、把 `kind=` 拿掉、恢復日記 `send` → 對應測試都要紅。
- [ ] 棘輪測試綠、budget 沒改。
- [ ] 全套 ≥ 6167 passed、0 failed。
