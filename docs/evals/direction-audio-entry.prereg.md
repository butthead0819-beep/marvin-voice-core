# Eval preregistration：未知意圖喚醒句的方向判斷（Audio Rescue 換模型）

決策：要不要把 Audio Rescue（叫了馬文、規則沒人接時，唯一會聽音檔的那一步）的模型從 `gemini-2.5-flash-lite` 換成 `gemini-3.5-flash`，作為使用者 10/22 提的「統一音訊入口」第一步。
兩個 arm 只換模型：同一個 `AudioRescueAgent`、同一份 manifest（bus 實際註冊的 agent）、同一段音檔、同一段 STT 文字。

資料（32 句，皆為本機音檔，不進 repo）：
- `records/rescue_wav/` 18 句：prod 上 Audio Rescue 實際救回並執行過的（點播/指令/查詢）。其中 11 句的 STT 原文已被 ZDR 清掉（scrubbed），只剩音檔。
- `records/wake_samples/` 14 句：叫了馬文、bus 沒人接、10/6 起的 log 對得到的句子（與上面重疊的 3 句已去重）。

標準答案（人工標，方向四類）：查詢 5／指令 8／點播 6／聊天 13。
模型輸出換算方向：music、find_song → 點播；playback_control、volume、now_playing → 指令；grounded_qa、game_knowledge → 查詢；放棄（just_chatting / 其他放棄原因）→ 聊天。
已知標註疑點：「起床工作」（標點播，prod 也當歌名放）、「要說到很小可以呀」「波首師傅的善後」（標聊天，聽不出指令）。

## Bars
- 查詢召回（標查詢的 5 句判成查詢的比例）：B ≥ 4/5 且 B ≥ A。未達＝3.5 在問句上沒有比較好，不換。
- 指令＋點播召回（14 句判到正確方向）：B ≥ 12/14 且 B ≥ A − 1。未達＝換了會讓糊掉的指令/點歌救不回來，不換。
- 聊天誤觸發（13 句被判成查詢/指令/點播的句數）：B ≤ max(A, 2)。未達＝換了會讓 Marvin 對旁人聊天亂動作，不換。
- 延遲（B 單次呼叫 p50，含音檔上傳）：≤ 4.0 s。未達＝改用 thinking 較低的設定重評，不直接換。
- 全部達標才換；任一未達照上面的「未達」處理。

## Arms
- A：`gemini-2.5-flash-lite`，prod 現行設定（AudioRescueAgent 預設，timeout 5 s 改為 15 s 以免逾時當成放棄而失真），per-item ≈ $0.0012（gstack arms 估，4000 in / 2000 out；實際音檔另計 $0.30/M）。
- B：`gemini-3.5-flash`，其他同 A，預設思考，per-item ≈ $0.0210（價目表無 3.5-flash，保守代用 3.6-flash 2027 價 $1.50/$7.50）。

## Decoys
- 13 句「聊天」：都是在講馬文（「馬文製造了一個空間讓大家可以待比較久」「馬文終於長大了」），不是對馬文下指令。它們是 prod 真實發生、bus 沒人接的句子，不是人造的。模型把它們判成任何動作都算失敗；B 在這組的表現就是「換了會不會亂插嘴」的證據。

## Stop rules
- 花費上限：$1.00（全部 32 句 × 2 arm 保守估約 $0.71）。
- 試算（每 arm 3 句）：B 有 ≥ 2 句呼叫失敗或逾時 → 停，先修 harness。
- `budget_exhausted` → 停。
- 付費呼叫一律經 `PaidUsageGuard.record(caller="eval_direction")` 進專案帳本（專案鐵則：付費必記帳）。

## Held-out exposure
| set | prior exposure | reservation |
|---|---|---|
| 無 | 本評估沒有 held-out 集；32 句全是 dev，`rescue_wav` 的 18 句過去已被 prod 的 flash-lite 看過（就是它判的） | 全部可讀 |
