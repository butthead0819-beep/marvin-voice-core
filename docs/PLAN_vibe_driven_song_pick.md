# 計劃書：讀空氣——日記氣氛 → DJ 口白／話題選歌

> 2026-10-18 與使用者討論後改寫。原版「VibePick：10 分鐘日誌驅動選歌」已作廢，理由見 §1。
> 接手前必讀：`CLAUDE.md`、記憶 `feedback-read-mood-dont-reveal`、`feedback-dj-observer-no-first-person`。

## 0. 鐵則（使用者定）

**Marvin 讀得到氣氛，但不能講出來、不能暗示知道**——像人類一樣不露餡地體會他人情緒。
氣氛只透過語助詞多寡、口語程度、句子長短、要不要跟著鬧來表現；有人情緒不好時不拿他的事開玩笑。
選歌可以被氣氛影響，但選歌理由／口白不准提情緒。

## 1. 為什麼改方向（數據，bot_main.log 10/4–10/18）

- **舊日記讀不出氣氛**：`ambient_diary` prompt 寫明「只保留有實質內容的訊息」，四行格式全是話題。
  13:20–13:30 逐字稿是 showay 抱怨隊友講不聽、狗與露大笑附和、後段沉默 4 分鐘；日記只寫「討論《古木騎兵》女主角」。
- **AssociativePick 幾乎沒在運作**：「LLM 選了 30 天內選過」233 次 vs 成功入隊 8 次（約 3%）；
  LLM 從全世界提名一直撞周杰倫/陳奕迅；成功那幾首關聯牽強（眼睛疲勞→《我的地盤》）。
- **mood_sensor 跑了沒人感受得到**：4 標籤（興奮 395/放鬆 122/低落 81/分歧 35），唯一用途是選歌 feelings 關鍵字 soft re-rank，候選池本來就小聽不出差別；且寫死 model ID。
- 原計劃書另有幾處跟現況不符：候選是 `round_size*3`=9 首非 10–15；`assemble_arc` 只認 `state_reason` 當 lead；
  「驗證口白 45–55 字」是測 LLM 輸出（mock 自己當 fixture）；「>50% 選歌無理由」查無出處。

## 2. 氣氛來源：日記加「氣氛」行

```
核心：…
摘要：…
氣氛：戲謔爭執｜showay不爽因被重複問     ← 基調必填｜情緒事件選填
顯著度：中
meme：…
```
- 基調：2–8 字、用 LLM 自己的話描述實際節奏與互動。**prompt 不給範例**——給了「熱絡、互相吐槽…」實測 3/3 段都抄成「熱絡」；拿掉範例後 5 段各不相同。
- 情緒事件：逐字稿明顯看得出來才寫，最多一件，「誰＋什麼情緒＋因為什麼」≤25 字；只准寫有發言者的名字（被提到沒發言的人用「隊友」代稱）。
  曾試「每人一句情緒＋原因」→ 會編情緒湊格子（「showay 理性因方案合理」），已否決。
- 本地統計（笑聲次數/發言數/沉默）實測無用：STT 很少轉出「哈哈」，不做。
- 離線重跑腳本：session scratchpad `replay_diary_mood.py`（走 llm_pool bus、唯讀 marvin.db）。

## 3. PR①（進行中，分支 `feat/dj-mood-hint`，worktree `dvb-mood-hint`）

A. 日記氣氛行：`marvin_prompts.ambient_diary` + `DiaryEntry.mood`。
C. DJ 口白內化：新純函式模組 `dj_mood_hint.py`
   - `latest_mood`：最新一筆有氣氛的日記，>25 分鐘過期
   - `build_mood_hint`：日記的人全不在場 → 不給；事件主角不在場 → 丟事件只留基調
   - 注入 `_fetch_dj_interjection_raw` 的 ctx（只影響主 LLM 路徑），標「只給你調整說話方式用，不准提到、轉述或暗示」
   - **防露餡硬檢查第一天就上**：口白與氣氛提示（去掉人名、標點後）有 ≥4 字連續相同，或兩邊都出現情緒詞（不爽/焦慮/心情…）→ 判「露餡」，走既有不合格 fallback（autopilot 退 quick 模板）
   - `dj_narration_log` 多記 `mood_hint`，可量洩漏率與注入率
D. 拔掉 mood_sensor（含 main_discord 接線、story_arc vibe_filter 呼叫、recommendation log 的 vibe 欄位）。
   後果：MoodAgent 的 mood 軸恆為預設「放鬆」→ BridgeAgent / ProactiveTopicAgent 的 `heavy` 禮讓閘永遠不觸發（原本要「低落＋低溫＋靜默≥60s」）。
   `music_recommender.vibe_filter` / `_vibe_boost` 成為死碼，留待另外決定。

驗收（上線 24h）：日記有氣氛行 ≥90%；`mood_hint` 注入的口白中「露餡」不合格率；人工聽感——同一段氣氛前後口白語氣有差。

## 4. PR②（待做）：AssociativePick ＋ VibePick 融成一條話題選歌

1. 輸入：最近 15 句逐字對話為主，最新日記 core（＋氣氛，僅內化）當背景
2. **先從本地候選池挑**（免費 LLM `router._call_llm tier="simple"`，只回 index 或 null），挑不到才走提名
3. 提名改成一次給 3 首、取第一首 30 天內沒選過的（治 233 次重複）
4. 兩條共用冷卻（15 分鐘）與 `associative_history`
5. 挑中的歌當 lead（`assemble_arc` 認 `state_reason` 之外也認話題理由）
6. 門檻：歌名或歌詞直接碰到話題才算，否則 null；選歌理由不准提情緒
7. 在場：日記 speakers 至少一人在場才用

待定：理由要不要進口白（走 `memory_match` 還是只記 log）——記憶規則「說接歌理由版已否決、歌是主角」。
