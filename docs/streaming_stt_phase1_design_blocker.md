# Volatile 串流 STT Phase 1 — 設計阻塞與待決議

> 2026-06-13 hot sprint 產出。基礎已建+測+live 驗證核心價值，但因架構衝突
> 暫停（`STT_STREAMING` 預設 OFF）。本文給未來 design review 的接手點。

## 目標

用 SpeechAnalyzer 串流 + 語意斷句（文字穩定即切）取代 VAD 純靜默計時的
0.8–3s 等待，砍 Time-to-first-voice。

## 已建成（OFF，可重啟即用，全測過）

| 檔案 | 角色 | 狀態 |
|---|---|---|
| `streaming_endpointer.py` | 文字穩定窗→提前切決策（純函式） | ✅ 測過 |
| `stream_stt_daemon.swift` / `_bin` | 暖模型常駐 daemon，stdin R/A/F、stdout volatile JSONL | ✅ 煙霧過 |
| `streaming_stt_session.py` | daemon volatile→endpointer→on_cut，共享 daemon 開機暖、ready 門、active_cut 路由 | ✅ 測過 |
| `discord_voice_engine.py` Sink 接線 | 單一講者佔用、early cut 鏡像 VAD、降級 | ✅ 測過 |

commits：cd37c30（基礎）、1944ddf（Sink 接線）、91ed984（開機暖+ready 門，修
冷載入 19s 滯後）、add58f1（daemon 硬 reset，修部分 merge）。

## live 驗證結論

**核心價值成立**：
- 喚醒 + 點歌全鏈正常（13:02 / 14:07 兩次：wake=True → music → 播歌）
- 語意斷句提前切確實比 VAD 早觸發（cut→喚醒 ~700ms）
- 冷載入滯後已除（開機暖模型一次）

**阻塞 bug：跨語句文字累積（merge）**——連續講話時，點歌句與其後內容被黏成
一個 cut。

## 根因：兩套分段哲學在同一條音訊上打架

Marvin 現有兩套「把音訊切成語句」的機制，philosophy 不相容：

1. **wake-check 快速通道**（既有）：每 0.6/1.2/1.8s 對 buffer 拍**非破壞性快照**
   偵測喚醒詞，**不消費 buffer**——語句在背景持續累積，wake 可在 span 中途命中。
2. **串流 daemon（新）**：一個 utterance 持續到「靜默切」或「語意切」才結束。

衝突場景（實測重現）：使用者連續唸字、中間無足夠靜默 →
- VAD 破壞性「靜默切」從未觸發
- wake-check 在中途快照抓到「馬文播放曹格的背叛」→ 喚醒+播歌（快照、buffer 沒清）
- daemon 把點歌句 + 後續 20s 內容當**同一連續 utterance** 累積 → 語意切吐出全部黏一起

補切句路徑（event-VAD / watchdog / daemon hard-reset 都試過）無法解，因為問題不在
「漏接 release」，而在 wake-check 的非破壞性快照與 daemon 的破壞性 utterance 是兩種
不可調和的分段語意。

## 待決議（design review 要回答的）

1. **誰是分段的單一事實來源**？選項：
   - (a) 串流 daemon 成為唯一斷句器，wake-check 退役 / 改成「在 daemon volatile 文字上
     比對喚醒詞」（統一到 daemon 的 utterance 邊界）
   - (b) wake 成功消費一段後，顯式 reset daemon span（把兩套接起來，但耦合）
   - (c) streaming 只服務**非喚醒長閒聊**（Phase 1 限縮版，放棄喚醒加速，避開衝突）
2. wake-check 的 600ms 快照延遲 vs 語意斷句延遲，哪個對喚醒句更快？若 wake-check 已夠快，
   streaming 對喚醒句的邊際價值可能不大 → 傾向 (c)。
3. 多講者：單 daemon 一次一句的限制要不要解（per-speaker daemon？成本）。

## 重啟方式

`run_bot.py` 設 `STT_STREAMING=true` 即重新接上（OFF 時零行為、daemon 不 spawn）。
但**在分段衝突解決前不要常開**——會在連續講話時產生 merge cut 污染非喚醒轉錄。

## 相關
- Phase 0 影子量測仍在收數據（`VOLATILE_SHADOW=true`，records/volatile_shadow.jsonl）
- VAD 設計規範見 CLAUDE.md〈VAD 層設計規範〉
