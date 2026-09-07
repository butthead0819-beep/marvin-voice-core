# TTS 發話策略：用規則取代 `protected` / `bypass_stream_mute` 旗標

狀態：**草案 / 待實作**　建立：2026-09-07

## 1. 問題

`play_tts()` 現在有一疊「靜音 / 丟句」守則，呼叫端靠命令式旗標逐一跳過：

| 旗標 | 意思 | 呼叫端要記得的事 |
|---|---|---|
| `protected` | 跳過 game_mode / Silence Gate / Interrupt / Load Drop | kwarg **是死的**，要手動 `self._tts_protected = True` + try/finally（15+ 處各寫一遍） |
| `bypass_stream_mute` | 再跳過 Stream Guard | 只有 join 招呼用 |
| `silent_during_stream` | 反過來：宣告「我是主動發言，串流中請靜音我」 | = `speak(proactive=...)` |
| `priority` | Load Drop 的佇列上限（0=無限 / 1=8s / 2=3s） | 數字沒有語意 |

痛點：
1. **加一道守則 = 每個逃生口都要記得檢查旗標**。2026-09-07 實測 join 招呼被 Stream / Interrupt / Hot-Chat 三道守則各丟一次，一道一道修（PR #84 / #86 / #87）。
2. **`protected` 把語意壓成一個 bit**：join 招呼、送客、summon 登場、遊戲主持、DJ 口白、`/say`、NemoClaw 轉述——全部 `protected=True`，但它們該有的行為其實不同（見 §4）。
3. **沒有真正的優先權**：只有「繞過 / 不繞過」，不能表達「A 蓋過 B」「等一個空檔」。
4. **`_should_mute_for_stream_guard` 已退化**成 `stream_mode and silent_during_stream`，`allow_hotswap` 是死參數。

## 2. 設計：SpeakKind + RoomState + 單一 policy

話語不再自己喊「跳過 X」。話語只宣告**它是什麼**，policy 依當下房間狀態決定怎麼播。

```
play_tts(text, kind: SpeakKind, *, emotion_tag=..., voice=..., force_macos=...)
         │
         ▼
    RoomState.snapshot()          # stream_mode / hot_chat / user_speaking / last_interrupted / mixer_load / game_mode
         │
         ▼
    decide(kind, room) -> Verdict  # 一張表，不是散落的 if
         │
    ┌────┴─────────────────────────────┐
  PLAY / PLAY_OVER / HOTSWAP    DEFER   DROP_TO_TEXT / DROP
```

`protected` 消失——它變成「`JOIN_GREETING` 那一列在所有逆境欄位都寫 PLAY」這個**規則結果**。呼叫端只寫 `kind=SpeakKind.JOIN_GREETING`。

### 2.1 RoomState（都是現成的值，只是收攏）

| 欄位 | 來源 |
|---|---|
| `stream_mode` | `self.stream_mode` |
| `hot_chat` | `self._room_mood_store.get(0).hot_chat` |
| `user_speaking` | `not await self._wait_for_user_silence()`（async，會等空檔） |
| `last_interrupted` | `self._tts_interrupted`（且 `already_in_channel`） |
| `mixer_load_s` | `self._mixer.tts_load_seconds()` |
| `game_mode` | `self.game_mode` |

### 2.2 Verdict 語意

| Verdict | 行為 |
|---|---|
| `PLAY` | 立刻推 mixer，滿音量 |
| `PLAY_OVER` | 立刻推，且 duck 音樂 / 背景（＝現在 `bypass_stream_mute` 的效果） |
| `HOTSWAP` | 串流中以短句注入（≤ `STREAM_BUDGET` 字，超字 → 依 fallback 欄位） |
| `DEFER(t)` | `await` 空檔最多 `t` 秒；等到 → `PLAY`，逾時 → 該列的 `defer_timeout` 欄位（`DROP` 或 `DROP_TO_TEXT`） |
| `DROP_TO_TEXT` | 不發聲，`active_text_channel.send(f"💬 {text}")` |
| `DROP` | 靜默丟棄（仍寫一行 log） |

`last_interrupted` 命中時的**副作用**（清 `_tts_interrupted`）在 policy 決定 `PLAY*` 後統一執行，不再藏在守則裡。

## 3. 遷移路徑（增量、可回退）

1. **✅ PR #88（landed）**：`_tts_suppressed()` —— 6 道守則收進單一 async 函式，`protected` 短路開頭。
2. **✅ PR #89**：`tts_speak_policy.py`（`SpeakKind` / `RoomState` / `Verdict` / `decide()` 純函式 + 窮舉測試）。`_tts_suppressed` 的 if 鏈換成 `decide()` 查表。`play_tts` / `speak` 新增 `kind=`；沒傳 → `_legacy_speak_kind()` 從舊旗標推導（相容墊片）。已改的呼叫點：`JOIN_GREETING`、`LEAVE_FAREWELL`、`STANDUP`/`JOKE`/`IMITATE`（後三者依 §5 定案降級成主動類）。
3. **⬜ 逐一改剩下 ~70 個呼叫點**：刪舊旗標、加 `kind=`（多數是 `WAKE_REPLY`，機械性）。
4. **⬜ 從簽名刪掉** `protected` / `bypass_stream_mute` / `silent_during_stream` / `allow_hotswap` / `hotswap_max_chars` / `priority` + `_legacy_speak_kind` 墊片。
5. **⬜ `_tts_protected`** 實例旗標退役（barge-in guard 改讀 policy / kind）。

### 使用者定案（2026-09-07）

- **`LEAVE_FAREWELL`**：可略（主動類，房間忙就不講）
- **`STANDUP` / `JOKE` / `IMITATE`**：降級成主動類（原本掛 protected 會蓋人講話）
- **`DEFER` 逾時**：→ `DROP_TO_TEXT`（reply 類補文字；proactive 類仍 `DROP`）

驗收：每階段全套 pytest 綠；`test_tts_suppressed_protected_bypasses_every_guard_at_once` 換成「每個 `PLAY_ALWAYS` kind 在全逆境下 policy 回 PLAY」的參數化契約測試。

## 4. Kind 分類（79 個呼叫點盤點）

> 依現行 flag 組合 + 觸發情境歸類。實作時逐點確認。

### 4.1 committed 事件（現在靠 `protected`，policy 應 PLAY-always）

| Kind | 呼叫點 | 說明 |
|---|---|---|
| `JOIN_GREETING` | `voice_controller.py:955` | 有人加入 → 司儀播報句。唯一現用 `bypass_stream_mute` |
| `LEAVE_FAREWELL` | `voice_controller.py:983`, `voice_controller_social.py:594` | 有人離開。**注意現行不對稱**：`:983` 沒帶 protected（可被丟），`social:594` 帶——遷移時要定案 |
| `SUMMON_INTRO` | `voice_controller_connection.py:762`, `voice_controller_commands.py:39` | `/summon` / auto-join 登場台詞 |
| `GAME_HOST` | `busted99_cog.py:254/900`, `turtle_soup_cog.py:167`, `game_cog.py:1313` | 遊戲主持，全走 `force_macos=True` |
| `DJ_NARRATION` | `music_cog.py:1496/3636`, `voice_controller_social.py` 的 dual interject | 歌曲間口白，全程 `_tts_protected` |
| `SELF_SAY` | `voice_controller.py:3525` (`/say`) | 使用者叫 Marvin 唸一句 |
| `EXTERNAL_RELAY` | `voice_controller.py:3027`(NemoClaw), `:3790`(reply) | 外部服務轉述，處理耗時佇列可能積壓 |

### 4.2 回應類（現在多為 `already_in_channel=True` 裸呼叫）

| Kind | 呼叫點 | policy 傾向 |
|---|---|---|
| `WAKE_REPLY` | `voice_controller.py:2090/2102/2118/2132/2147/2267/2298/2362`, `:3499/3588/3929/3954`, `grounded_qa_agent.py:261/269` | `DEFER(短)`：使用者剛講完就等一下再答；被打斷 → DROP 續句 |
| `WAKE_ACK` | `voice_controller.py:2681/2726` | filler「嗯?」，`allow_hotswap=urgent`。串流中 HOTSWAP，忙 → DROP |
| `RESULT_NOTICE` | `voice_controller.py:1057/4069`, `:3962` | 操作結果（音樂生成失敗等）。`DEFER` 或 `DROP_TO_TEXT` |
| `RECALL_CONFIRM` | `voice_controller.py:2151`（「好，不記了」） | 短確認，`DEFER(短)` |

### 4.3 主動類（現在 `silent_during_stream=True` / `proactive=True`）

| Kind | 呼叫點 | policy 傾向 |
|---|---|---|
| `PROACTIVE_TOPIC` | `voice_controller_system_loops.py:100/289`, `voice_controller_social.py:397` | 冷場找話題。stream/hot_chat → DROP |
| `PROACTIVE_MANZAI` | `voice_controller_social.py:431/509`, `spontaneous_manzai_agent.py:112` | 雙人吐槽 dual。hot_chat → DROP |
| `PROACTIVE_MOCK` | `voice_controller.py:1787` | 延遲嘲諷。stream → DROP |
| `MEMORY_CALLBACK` | `memory_callback_agent.py:227` | 「你之前說要 X」。hot_chat → DEFER |
| `NEWS` | `voice_controller_system_loops.py:319` | idle 報新聞。user_speaking → DROP（現行就是被 Silence Gate 丟） |
| `SOCIAL_FILLER` | `voice_controller.py:1034`(社交補位), `:4246`(頻率共鳴) | hot_chat → DROP |
| `STANDUP` / `JOKE` / `IMITATE` | `voice_controller_social.py:477/551/565` | 現在 `protected=True` 但其實是主動表演——**遷移時該降級**成主動類，別讓它蓋人講話 |

### 4.4 系統類

| Kind | 呼叫點 | policy |
|---|---|---|
| `SYSTEM_ALERT` | `voice_controller.py:1005`（額度耗盡撤離） | PLAY-always（關機前最後通知） |
| `LONG_READ` | `voice_controller.py:876`（朗讀長文，截 300 字） | `DEFER`；被打斷即停 |

## 5. 開放問題

1. **`LEAVE_FAREWELL` 定案**：committed（一定播）還是主動（房間忙可略）？現行程式碼自相矛盾。
2. **`STANDUP`/`JOKE`/`IMITATE` 降級**：現在掛 `protected` 會蓋過人講話，體感上像 Marvin 插嘴。改成主動類 → hot_chat / user_speaking 時 DROP。要確認這是想要的。
3. **`DEFER` 的實作**：`_wait_for_user_silence()` 已經會等，但沒有「逾時後 DROP vs DROP_TO_TEXT」的分支。verdict 要帶 timeout 與逾時行為。
4. **`force_macos` / `voice` / `emotion_tag`**：這些是「怎麼唸」不是「要不要唸」，維持獨立參數，不進 kind。
5. **SpeakBus 整合**：SpeakBus 的 bid 得標後 handler 仍呼叫 `play_tts` → 仍走這張表。是否讓 SpeakBid 直接帶 `SpeakKind` + 讓 policy 的 priority 供 SpeakBus 排序？→ 獨立議題，不擋本 spec。

## 6. 非目標

- **不動** SpeakBus 的仲裁邏輯（tick 觸發、multiplier）。本 spec 只處理「決定要發話之後，播放層怎麼裁決」。
- **不動** `_stream_tts_to_mixer` / hotswap 注入 / mixer duck 機制本身。
- Companion Radar（env-gated 的人類 veto）維持獨立，不進 kind 表——它是外部否決，不是內部策略。

## 附：相關記憶

- `join_greeting_playtts_guard_stack` — 三道守則各丟一次的事故 + `_tts_suppressed` 收斂
- `p1_conflicts_clarification` — `protected=` kwarg 是死的
- `speakbus_and_survival` — SpeakBus bid 架構
