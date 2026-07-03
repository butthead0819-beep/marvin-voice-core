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
LINE_BUDGET = 4254      # 實測 4254（2026-07-03 方案A per-speaker 序列化 +21：worker body Extract Method 成 _process_query_task（行為不變、legacy 與 SpeakerDispatcher 共用）+ producer 分流 4 行；邏輯在 speaker_dispatch.py）
METHOD_BUDGET = 91      # VoiceController 自身定義的 method 數；新「功能」別在這加 method
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
