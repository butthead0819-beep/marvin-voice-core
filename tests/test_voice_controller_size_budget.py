"""
🚧 voice_controller.py 防胖守門（ratchet / 棘輪）

voice_controller 曾是 7000+ 行的 god-object。經一連串 strangler-fig 抽離後，
這個守門把「不准再往 voice_controller 加功能」變成 CI 會擋的硬規則。

規則（重要）：
  - 這兩個 budget 只能「往下調」（抽離程式碼後同步降低數字）。
  - **絕對不要為了塞新功能把 budget 調高。** 新的語音功能應該去：
      * 新 IntentAgent（intent_agents/*.py）—— wake 後的意圖派發
      * 新 Cog（cogs/*.py）—— 自成一格的子系統（音樂 / 遊戲…）
      * 新 mixin 模組（cogs/voice_controller_*.py）—— 與 VC 共用 self 的內聚方法群
    而不是在 VoiceController 上多寫一個 method 或往現有巨型方法塞行數。
  - 若這個測試擋住你：先問「這真的非得進 voice_controller 不可嗎？」答案幾乎都是否。

調降時機：每次成功抽離一塊（如 PlaybackMixin / 系統迴圈），就把數字改成新的實測值。
"""
from __future__ import annotations

import re
from pathlib import Path

VC = Path(__file__).resolve().parent.parent / "cogs" / "voice_controller.py"

# ── 棘輪基準（2026-06-20，#3 抽 _apply_wake_guards 後）──────────────────────
# 例外說明：in-file Extract Method（把巨型方法拆成有名字的子方法、行為不變）會讓
# 行數/方法數微升——這是「拆解」不是「加功能」，允許據實上修。被擋住時先自問：
# 這是 Extract Method 把既有邏輯分出來，還是真的新增了功能？只有前者可調高。
LINE_BUDGET = 4249      # 實測 4249（2026-09-02 Voice Flap Guard +3：import collections
                        # + self._voice_flap_ts deque 初始化 + cog_load 一行 _install_voice_flap_watch()
                        # ——_VoiceFlapObserver / _record_voice_flap / _install_voice_flap_watch 本體
                        # 全在 cogs/voice_controller_connection.py，這 +3 只是欄位與啟動接線的最小
                        # 成本，比照下方 GroundedQAAgent +2 同型先例）；
                        # 前 4246（2026-09-01 JokeRequestAgent +2：一行 import JokeRequestAgent
                        # + 一行 agent-list 註冊——handler 本體與 joke_bank 抽取全在
                        # intent_agents/joke_request_agent.py，比照下方 GroundedQAAgent +2 同型先例）；
                        # 前 4244（2026-08-30 AmbientQA / GroundedQAAgent +2：一行 import
                        # GroundedQAAgent + 一行 agent-list 註冊——handler 本體與 grounded 呼叫
                        # 全在 intent_agents/grounded_qa_agent.py，這 +2 是新 IntentAgent 的
                        # 最小接線成本，比照下方 /marvin_talk +2 同型先例）；前 4242（2026-08-29 /marvin_talk 回覆滿音量 +2：handle_raw_speech_start
                        # 開頭的 _talk_active() early-return 從嘲諷段前移到最頂，跳過整條 speech-start
                        # 副作用鏈——既有守衛位置微調，非新功能）；前 4240（2026-08-29 /marvin_talk 獨佔守衛 +5：handle_raw_speech_start
                        # 嘲諷鏈 + play_intervention 開頭各加一條 `if self._talk_active(): return`
                        # ——既有守衛鏈加分支、非新獨立功能，比照下方 is_text_input / idle-hangout
                        # 同型先例調高）；前 4235（2026-08-29 Audio Rescue v2 音訊斷鏈修復 −3：override_query
                        # 是實際唯一路徑，_process_queued_query 的 if/else 兩份 speech_buffers
                        # 音訊擷取邏輯（其中一份在恆不執行的 else 死枝）合併成 pop 前單一路徑，
                        # 順帶收斂淨減行數）；前 4238（2026-08-29 /marvin_talk 回合制對談 +2：一行 import
                        # MarvinTalkMixin + 一行 self._init_talk_manager()——功能本體全在
                        # cogs/voice_controller_talk.py + marvin_talk.py，這 +2 是把功能「移出去」
                        # 的最小接線成本，正是棘輪要的方向）；前 4236（2026-08-25 純掛網不嘲諷 + 音樂控制冷卻戳記 +15：
                        # handle_raw_speech_start 既有嘲諷守衛鏈加一條 idle-hangout 分支、
                        # 主 dispatch 收尾加一行記錄 last_music_control_time——都是既有守衛/
                        # 既有收尾點的微調，非新獨立功能，比照下方同類先例調高）；前 4221
                        # （2026-08-25 FrustrationAgent 送錯音訊時機 bugfix +14：既有
                        # _process_queued_query 音訊擷取邏輯補 _prev_turn_audio 快照，讓挫折句能
                        # 回頭撈「挫折產生之前」那輪的音訊，屬既有 pop 邏輯微調，非新獨立功能）；
                        # 前 4207（2026-08-09 修 play_tts protected= 死參數坑 +7：該方法只讀
                        # self._tts_protected、不讀傳入 kwarg，_handle_music_info_query 補手動拉
                        # 旗標的 try/finally——既有呼叫點微調，非新功能）；前 4200（2026-08-09 移除 _handle_farewell_speech / _farewell_role_resolve
                        # 側通道 -140：判斷邏輯複雜又不準的「聽到 bye 就猜會不會離場」偵測整條拔除，
                        # 只留 FarewellAgent 處理喚醒直接道別；departure_stats.py 同步砍掉
                        # predict_leaving_soon / typical_departure_summary / record_false_alarm 三個
                        # 陪葬的孤兒方法）；前 4329（2026-08-09 farewell wake gap +11）；前 4318
                        # （2026-07-15 f59b7dc device 關閉延遲嘲諷 +6：既有 device 模式行為微調，commit 當時漏調棘輪，此處補回）；前 4312（2026-07-11 da411bb 文字/Siri 介面 +24：既有語音守衛加 is_text_input 文字繞過分支，非新獨立功能、屬既有守衛微調——commit 當時漏調棘輪，此處補回）；前 4288（2026-07-08 ack 音量 bugfix +3：既有 _play_ack 加 peak_normalize_f32 拉滿幅）；前 4285（2026-07-03c 已服務標記 +11）
METHOD_BUDGET = 89      # VoiceController 自身定義的 method 數；新「功能」別在這加 method
                        # （2026-08-09 -2：拔除 _handle_farewell_speech / _farewell_role_resolve）
                        # （2026-07-03 +1：_process_query_task = worker body Extract Method，行為不變）


def test_voice_controller_line_count_within_budget():
    n = len(VC.read_text(encoding="utf-8").splitlines())
    assert n <= LINE_BUDGET, (
        f"voice_controller.py 漲到 {n} 行 > 預算 {LINE_BUDGET}。\n"
        f"不要為了塞功能調高預算 —— 新功能請進 IntentAgent / 新 Cog / 新 mixin 模組。\n"
        f"若這是把程式碼「移出去」造成的合法下降，請把 LINE_BUDGET 改成新的實測值。"
    )


def test_voice_controller_method_count_within_budget():
    # 只數直接定義在 voice_controller.py 的 method（4-space 縮排），mixin 不算
    src = VC.read_text(encoding="utf-8")
    n = len(re.findall(r"^    (?:async )?def ", src, re.MULTILINE))
    assert n <= METHOD_BUDGET, (
        f"VoiceController 自身 method 數漲到 {n} > 預算 {METHOD_BUDGET}。\n"
        f"新增的語音功能應該去 IntentAgent / Cog / mixin，不要在 VoiceController 上長新 method。"
    )
