"""DJ 串場「這輪要不要講、講什麼」協調順序的顯式化入口（Phase A）。

## 背景

「DJ 在歌與歌之間要講什麼話」這件事，實際邏輯散落在至少 8 個獨立檔案
（dj_topic_selector / dj_life_context / dj_story_arc / dj_social_affinity /
dj_comedy_fallback / dj_prompt_builder / dj_tail_schedule / joke_bank），
真正的呼叫順序則寫死在 `cogs/music_cog_tail_dj.py::_run_tail_dj` 跟
`cogs/music_cog_dj_lyrics.py::_fetch_dj_interjection_raw` 兩支方法的程式碼裡，
要理解「Marvin 這輪為什麼講了這句話」得自己讀 8 個檔案拼順序。

這個模組**不改變任何行為**，只是把其中「輸入輸出乾淨、無 Discord/LLM/TTS
side effect」的兩段決策——尾段點火時機、話題來源挑選——原封不動包一層，
用清楚的函式名稱 + docstring 顯式標出「這一步在幹嘛、為什麼是這個順序、
什麼情況會跳過誰」。

`_fetch_dj_interjection_raw` 裡其餘會碰網路/LLM/TTS/self 狀態的步驟
（歌詞抓取、LLM 生文案、TTS 預渲染、joke_bank 冷卻時間戳讀寫等）**沒有**
包進來——那些牽涉 IO、無法在不動 `music_cog_dj_lyrics.py` 的前提下抽成
純函式做 characterization test，屬於 Phase B（真的合併/搬動檔案時）才處理
的範圍。下面 `NARRATION_TEXT_CASCADE` 只是把那段的優先順序「寫下來」，
供人類/下一個改動者對照，不是可執行的邏輯替代品。

## 目前實際呼叫順序（讀 `_run_tail_dj` + `_fetch_dj_interjection_raw` 得出）

1. **要不要點火**（`_run_tail_dj`）：duration 已知 → 扣掉 highlight_start_s
   位移 → 丟給 `dj_tail_schedule.tail_dj_fire_delay` 算「離歌尾還有幾秒該
   點火」。None 就整輪放棄、退回舊路（開頭混播 / `_maybe_play_dj_interjection`
   的既有呼叫點）。→ 對應 `compute_tail_fire_delay()`。
2. 點火後 re-check（stream 是否已停 / 有沒有被 skip / 歌是否已切換）、
   再從 `stream_queue[0]` 現抓下一首（不能在點火前綁定，autopilot 常常
   點火當下才把下一首排進 queue）。
3. PuckMixer（esp32_edge_mix）crossfade 訊號 fire-and-forget 送出，跟
   本地 Discord mixer 的口白邏輯完全獨立、互不影響。
4. 下一首背景 preload（`_start_music_preload`）——**在** DJ meta 判斷之前
   就先做，避免 DJ 沒話講時連帶拖慢下一首換源。
5. 取下一首的 DJ meta（`_resolve_tail_dj_meta` → 沒 prefetch 過就現場呼叫
   `_fetch_dj_interjection_raw`），meta 裡的文字是怎麼決定的：
   a. `_lane == 'story_arc'`：直接用 `/story_arc_prepare` 階段已經生成+
      TTS 預渲染好的台詞（dj_story_arc.py 產出），完全跳過下面所有步驟。
   b. `_lane == 'themed'`：直接用主題歌單策展時寫好的選歌理由
      （`_themed_dj_text`），同樣跳過 LLM。
   c. 話題來源挑選（**跟要不要講的「文字」是兩件事**——這一步只決定
      「這輪如果要講，素材從哪來」）：`dj_topic_selector.select_mode()`
      依生活素材（dj_life_context 抽出、事件主角要在場）→ 在場興趣 →
      情緒高光 → 新聞 → 都沒有時在 conversation/prev_song/atmosphere/quick
      間本地輪替，挑一個 mode。若 autopilot 有算好推薦理由
      （`_autopilot_pick_reason`）且 mode 落在 quick/atmosphere 這兩個
      「敬陪末座」的 fallback，直接蓋掉、改用 "reason"（好料不該被輪替
      吃掉）。→ 對應 `select_narration_mode()`。
   d. 頻道熱度/社交親密度上下文（dj_social_affinity：連播偵測、
      社交親近度、環境氛圍字串）併入 LLM prompt 的 ctx，不影響上面的
      mode 選擇，只影響最終文案怎麼寫。
   e. **文字來源優先序**（尚未抽成純函式，見上方模組說明）：
      story_arc（見 a）> themed（見 b）> joke_bank 命中（頻道不是熱聊中
      + 距上次講笑話超過冷卻 + 拼音撞中 hook）> mode=="quick" 時的本地
      固定模板（`_quick_segue_text`，零 LLM）> LLM 生成
      （`dj_prompt_builder` 組的 prompt，經 `bot.router` 呼叫）> LLM 空手
      或不合格時，Marvin 自選歌退回 `_autopilot_dj_phrase` 模板池 >
      仍無效則退回「DJ Marvin為你帶來《X》」固定格式報幕（保底、永不失敗）。
   f. 最終文字過 `tts_length_policy.truncate_for_tts` 長度閘門，再送 TTS
      預渲染成音檔。
6. 疊播口白（`_maybe_play_dj_interjection`）+ 轉場音效（`_play_dj_tail_sfx`，
   目前整段被暫停），標記 `next_info['_dj_played_in_tail'] = True`。

## 使用方式

這個模組目前**沒有任何呼叫點**接它——`music_cog_tail_dj.py` /
`music_cog_dj_lyrics.py` 仍直接呼叫底層模組，行為完全不變。是否要讓
`_run_tail_dj` / `_fetch_dj_interjection_raw` 改叫這裡的函式取代自己的
邏輯，是 Phase B 的事。
"""
from __future__ import annotations

from dj_tail_schedule import tail_dj_fire_delay
from dj_topic_selector import TopicCooldownStore, select_mode

# 與 cogs/music_cog_tail_dj.py 的 _DJ_TAIL_LEAD_S 同值——尾段疊播窗口寬度，
# 不在這裡重新定義成 import（避免跟主檔互相 import 造成循環，同該檔案
# docstring 講的 _get_puck_client() local import 理由），呼叫端仍應自己
# 傳目前生效的 lead_s，這裡的預設值只是方便單獨測試/探索時有個合理起點。
_DEFAULT_TAIL_LEAD_S = 8.0


def compute_tail_fire_delay(
    duration_s: float | None,
    elapsed_s: float,
    *,
    highlight_start_s: float | None = None,
    lead_s: float = _DEFAULT_TAIL_LEAD_S,
) -> float | None:
    """[Step 1] 這輪尾段串場「要不要點火、還要等幾秒」——原樣包 `_run_tail_dj`
    開頭那段時間軸換算，只是把它從方法內的一段程式碼抽成獨立可測的函式。

    duration 未知（None/0）直接放棄（呼叫端該退回舊行為，見
    `_run_tail_dj` 開頭的 early return + log）。有 highlight_start_s
    （精華起播位移了實際播放時間軸）時，duration 要先扣掉這段位移，
    否則會把「離結尾還有多久」算得太樂觀——這行為原樣照抄
    `_run_tail_dj`，不是這裡新想的。

    真正「還要等幾秒才點火」的計算全權交給 `dj_tail_schedule.tail_dj_fire_delay`
    （歌太短 / 已經過窗一樣回 None），這裡不重算一份。

    回傳 None 時，呼叫端該做的事跟 `_run_tail_dj` 一樣：整輪放棄尾段串場，
    退回「混進下一首開頭」或 `_maybe_play_dj_interjection` 的既有路徑
    ——這件事本身仍是呼叫端的責任，這個函式只回答「現在」要不要點火。
    """
    if not duration_s:
        return None
    if highlight_start_s:
        duration_s = max(0.0, duration_s - highlight_start_s)
    return tail_dj_fire_delay(duration_s, elapsed_s, lead_s=lead_s)


def select_narration_mode(
    *,
    life,
    interests,
    topic_store: TopicCooldownStore,
    present_members=None,
    has_conversation: bool = False,
    has_prev_song: bool = False,
    emotional_highlights=None,
    news_items=None,
    autopilot_reason: str = "",
) -> tuple[str | None, str]:
    """[Step 5c] 這輪串場的話題素材從哪來——原樣包
    `_fetch_dj_interjection_raw` 裡「呼叫 dj_topic_selector.select_mode
    →autopilot 理由覆蓋 quick/atmosphere」這兩步的固定組合。

    優先序（真正的挑選邏輯在 `dj_topic_selector.select_mode` 裡，這裡不
    重複實作）：近期生活（主角要在場）→ 在場興趣 → 情緒高光 → 新聞 →
    conversation/prev_song/atmosphere/quick 本地輪替。

    autopilot_reason 覆蓋規則原樣照抄 `_fetch_dj_interjection_raw`：只有
    Marvin 自己選歌才會算出 `_autopilot_pick_reason`；只在 select_mode
    選到 quick 或 atmosphere 這兩個「沒有具體話題可用」的墊底 fallback
    時才蓋掉，換成有憑有據的推薦理由（mode="reason"）——不搶 life/interest/
    emotional_highlight/news/conversation/prev_song 這些已經挑到具體
    素材的 mode。

    回傳 (topic_text, mode)，跟 `select_mode` 的回傳形狀一致，mode
    多一種 "reason" 是這一步疊加上去的，不是 `select_mode` 本身會回的值。
    """
    topic, mode = select_mode(
        life,
        interests,
        topic_store,
        present_members=present_members,
        has_conversation=has_conversation,
        has_prev_song=has_prev_song,
        emotional_highlights=emotional_highlights,
        news_items=news_items,
    )
    if autopilot_reason and mode in ("quick", "atmosphere"):
        mode = "reason"
    return topic, mode


# [Step 5e 文件化] 見模組開頭「目前實際呼叫順序」第 5e 點——這段優先序目前
# 只存在於 `_fetch_dj_interjection_raw` 的一連串 if/elif 裡（joke_bank
# 冷卻時間戳讀寫、LLM 呼叫、TTS 生成都是 side effect，無法在不動那支方法
# 的前提下抽成可 characterization test 的純函式），這裡先用一份唯讀常數
# 把順序「寫下來」，讓下一個改動者不用重新讀一次整支方法才拼得出順序。
# Phase B 真的搬動 `music_cog_dj_lyrics.py` 時，可以把這份常數換成真正
# 驅動邏輯的來源（現在反過來，是靠讀 code 手動謄寫這份常數）。
NARRATION_TEXT_CASCADE = (
    "story_arc",       # info['_lane']=='story_arc'：用已預渲染好的台詞，跳過以下全部
    "themed",          # info['_lane']=='themed'：用策展理由，跳過以下全部
    "joke_bank",        # 非熱聊 + 冷卻已過 + 下一首歌名拼音撞中 hook
    "quick_template",  # mode=="quick" 且以上都沒命中：本地固定模板，零 LLM
    "llm",             # 以上都沒有 → dj_prompt_builder 組 prompt，走 bot.router
    "autopilot_template",  # LLM 空手/不合格 且是 Marvin 自選歌：_autopilot_dj_phrase 模板池
    "fixed_announcement",  # 以上皆無效的保底：「DJ Marvin為你帶來《X》」固定格式，永不失敗
)
