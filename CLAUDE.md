## 核心原則

**用繁體中文回覆。**

**謹慎優先、判斷力次之。** 以下是原則，不是死規則——瑣碎任務直接判斷，拿不準或有取捨時把假設、疑慮、多種解法攤開講，不要默默選一個。

1. **想清楚再寫**：不確定就問；有更簡單的做法就直說、該推回就推回。
2. **最少必要改動**：只寫解決問題所需的程式碼，不加沒被要求的功能/彈性/錯誤處理；只動非動不可的地方，不順手重構、不清無關的死碼（可以提出來但別動）。
3. **先驗證，再交付**：明確的 bug fix 或邏輯清楚的小改動，先寫失敗測試再寫實作是預設好習慣；牽涉多檔案、跨模組、或有探索空間（多方案不知道哪個最優）時，先講清楚驗收標準，用可機器判斷的方式（pytest / 明確條件）驗證過再算完成。要不要開 loopkit/worktree、commit 怎麼分——照任務規模判斷即可。
4. **收尾一行結論**（固定格式，供 Marvin HUD 直接解析，格式本身不可省略）：
   回應最後一行寫 `🏁 <15字內：處理了什麼問題> — <30字內：結果或下一步>`，講「處理了什麼」不要講「怎麼做的」。純聊天/還在釐清需求時可整行省略。

## 這個專案

Marvin，分三條分支：
1. **實體化**（Pi satellite / ESP32 puck satellite / HUD display）：把 Marvin 帶出 Discord，變成實體裝置
2. **Marvin DJ**：音樂播放分支，主打 autopilot DJ——懂口味、隨氣氛策展播放
3. **Marvin Discord**：每天陪伴所有人的 bot，負責記錄與策展（記得說過什麼、Full voice I/O）

三者共用同一條語音 pipeline 骨幹：`Discord Audio Sink → VAD → STT → Cleaner LLM → IntentBus → handler`，每層靠 Protocol 介面解耦，優雅降級（單一服務失敗不中斷整條流水線）。改動時先想清楚是動骨幹（影響全部分支）還是動單一分支。

各層的鎖範圍、閾值公式、Protocol 介面、async 安全等具體約束，多半是踩過真實 bug（CryptoError 風暴、busy-spin 凍結等）後留下的——改動這些之前**直接讀對應原始碼**（`protocols.py`、`intent_agents/base.py` docstring、各層現有實作），不要憑印象改。有疑慮就把方案攤開問，別自己猜一個看起來合理的版本。

新增/落地 IntentAgent 後，順手把對應的 intent_type 加進 `agent_gaps_resolved.json`（見 `scripts/analyze_agent_gaps.py` 開頭 docstring）——這份清單沒同步更新，`intent_clusters.json` 的每日/手動 gap clustering 會一直把已經有 agent 的東西誤標 `ready_to_implement`（2026-08-08 實測踩到：`agent_gaps_resolved.json` 從 6/7 後兩個月沒更新，漏了 5 個之後落地的 agent）。

**第三方函式庫（discord.py 等）回呼進來的 handler，不准直接呼叫「安裝這個回呼的那個函式」**——只能設旗標/標記狀態變髒，交給既有巡邏迴圈處理。callback 回頭觸發自己的安裝點＝隱形無窮迴圈：2026-09-17 事故正是 `arm_mixer` 的 `after=` 自癒 callback 無條件重呼叫「安裝它自己」的那個函式，一分鐘 223 次灌爆 voice websocket，被 Discord 4021 踢線 17 小時才查到根因（見 f518c97 / `tests/test_mixer_rearm_storm.py`）。

## 任務執行：實作發包給 sonnet

**確定要改什麼之後，實作交給 `claude -p --model sonnet --permission-mode acceptEdits` 跑，不要自己一行行寫。** 把任務描述寫成檔案再餵（`"$(cat 任務檔)"`），背景跑。

發包前我要先做完的事（這些不外包，外包會失真）：讀懂相關原始碼、定出設計、把驗收標準寫死。**任務檔要把設計定案講到沒有發揮空間**，並明講「有疑問就停下來回報，不要猜」——sonnet 自己發揮出來的設計通常要重做。

**收回來一定要自己驗，不能看它說綠就算數**（2026-09-17 實測兩種假綠）：
1. `cat` 測試檔——看它有沒有把被測邏輯複製一份進測試檔裡驗算自己（假測試，改實作也不會紅）
2. `git diff` 逐行看過，確認沒偷改設計、沒順手重構
3. **測試由我自己跑**：sonnet 在這個 sandbox 跑 `./venv_simon/bin/python` 會被權限擋，它會卡住或偷偷 fallback 到系統 python3
4. 沒走 TDD 的改動要做 mutation check：突變關鍵常數/條件確認測試會紅，沒紅就是有死角

它卡住沒做完就自己接手，不要重複發包。

## Skill routing

請求符合現有 skill 就用 Skill tool 呼叫，拿不準就呼叫。常見對應：產品發想→/office-hours、架構→/plan-eng-review、bug→/investigate、QA→/qa、code review→/review、視覺→/design-review、上線→/ship、存/復原上下文→/context-save /context-restore。
