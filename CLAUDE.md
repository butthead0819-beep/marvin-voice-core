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

## Skill routing

請求符合現有 skill 就用 Skill tool 呼叫，拿不準就呼叫。常見對應：產品發想→/office-hours、架構→/plan-eng-review、bug→/investigate、QA→/qa、code review→/review、視覺→/design-review、上線→/ship、存/復原上下文→/context-save /context-restore。
