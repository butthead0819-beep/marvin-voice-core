"""
🚧 voice_controller.py 防胖守門（ratchet / 棘輪）

voice_controller 曾是 7000+ 行的 god-object。經一連串 strangler-fig 抽離後，
這個守門把「不准再往 voice_controller 加功能」變成 CI 會擋的硬規則。

規則（重要）：
  - 所有 budget 只能「往下調」（抽離程式碼後同步改成新實測值）。
  - **絕對不要為了塞新功能調高 budget。** 新的語音功能應該去：
      * 新 IntentAgent（intent_agents/*.py）—— wake 後的意圖派發
      * 新 Cog（cogs/*.py）—— 自成一格的子系統（音樂 / 遊戲…）
      * 新 mixin 模組（cogs/voice_controller_*.py）—— 與 VC 共用 self 的內聚方法群
      * 純函式模組（如 etd_clean_reuse.py）—— 決策邏輯抽成可單測的 pure core
  - **不要壓行來過關**：量的是 AST statement 數，把兩行併成一行不會變少。
  - 若這個測試擋住你：先問「這真的非得進 voice_controller 不可嗎？」答案幾乎都是否。
  - 例外：in-file Extract Method（巨型方法拆出有名字的子方法、行為不變）會讓 method 數 /
    statement 數微升——這是拆解不是加功能，允許據實上修，並同步下修 FROZEN_METHODS。

2026-09-29 從行數改為 AST 量測（見 core_size_metrics.py）。行數預算時代的問題：
  - 每次接線 +2 都要在這裡寫一段辯護，預算註解長到 ~40 行；
  - music_cog 那邊頂滿後出現為過關壓行（48069ba）。
import 不計入 statement，所以新 IntentAgent 的接線（一行 import + agent-list 一個元素）= 0，
不用再調預算。四個指標：
  1. STATEMENT_BUDGET：整檔 statement 數（import 不算）
  2. METHOD_BUDGET：VoiceController 自身 method 數
  3. SELF_ATTR_BUDGET：檔內被賦值的 self.X 名稱數（共用可變狀態＝耦合）
  4. 單一 method 上限：新 method ≤ NEW_METHOD_MAX；已超標的大 method 凍結在現值只准縮
     （縮了就把 FROZEN_METHODS 的數字改成新實測值；縮到 ≤ NEW_METHOD_MAX 就刪掉那行）

行數演進（舊指標，留作歷史，細節見 git log -- 本檔）：7000+ → 2026-06-20 抽
_apply_wake_guards 後 ~4285 → 2026-09-25 worker cleaner 搬去 etd_clean_reuse 後 4163。
"""
from __future__ import annotations

from pathlib import Path

from core_size_metrics import method_statements, self_attrs, statement_count

VC = Path(__file__).resolve().parent.parent / "cogs" / "voice_controller.py"
CLASS = "VoiceController"

# ── 棘輪基準（2026-09-29 改 AST 量測時的實測值）──────────────────────────────
STATEMENT_BUDGET = 2200
METHOD_BUDGET = 87
SELF_ATTR_BUDGET = 152

NEW_METHOD_MAX = 40
FROZEN_METHODS = {  # 已超過 NEW_METHOD_MAX 的既有大 method：只准縮
    "handle_stt_result": 187,
    "__init__": 168,
    "_stream_response": 151,
    "_process_queued_query": 119,
    "_apply_wake_guards": 79,
    "process_debounced_speech": 64,
    "handle_raw_speech_start": 58,
    "_cot_filter_stream": 57,
    "_handle_nemoclaw_query": 51,
    "on_voice_state_update": 42,
}

_HINT = (
    "不要為了塞功能調高預算 —— 新功能請進 IntentAgent / 新 Cog / mixin / 純函式模組。\n"
    "若這是把程式碼「移出去」造成的合法下降，請把 budget 改成新的實測值。"
)


def test_voice_controller_statement_count_within_budget():
    n = statement_count(VC)
    assert n <= STATEMENT_BUDGET, f"voice_controller.py statement 數 {n} > 預算 {STATEMENT_BUDGET}。\n{_HINT}"


def test_voice_controller_method_count_within_budget():
    n = len(method_statements(VC, CLASS))
    assert n <= METHOD_BUDGET, f"VoiceController 自身 method 數 {n} > 預算 {METHOD_BUDGET}。\n{_HINT}"


def test_voice_controller_self_attr_count_within_budget():
    n = len(self_attrs(VC))
    assert n <= SELF_ATTR_BUDGET, (
        f"voice_controller.py 賦值的 self.X 屬性 {n} 個 > 預算 {SELF_ATTR_BUDGET}。\n"
        f"新狀態請放進功能自己的物件/模組，不要再往 VoiceController 的共用 self 上掛。"
    )


def test_voice_controller_method_size_within_budget():
    over = {
        name: (n, FROZEN_METHODS.get(name, NEW_METHOD_MAX))
        for name, n in method_statements(VC, CLASS).items()
        if n > FROZEN_METHODS.get(name, NEW_METHOD_MAX)
    }
    assert not over, (
        f"method statement 數超標 {{名稱: (實測, 上限)}}：{over}\n"
        f"大 method 只准縮不准長 —— 新邏輯抽成獨立 method 或純函式模組。"
    )
