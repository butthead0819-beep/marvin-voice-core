"""
🚧 music_cog.py 防胖守門（ratchet / 棘輪）

MusicCog 經 Phase 1-8 抽 mixin（2026-09-16，4863 行/128 method → 1527 行/29 method）後，
這個守門擋的是「往 MusicCog 本體長功能」。

規則（重要）：
  - 所有 budget 只能「往下調」（抽離程式碼後同步改成新實測值）。
  - **絕對不要為了塞新功能調高 budget。** 新的音樂功能應該去：
      * 新 IntentAgent（intent_agents/*.py）
      * 新 mixin 模組（cogs/music_cog_*.py）—— 與 MusicCog 共用 self 的內聚方法群
      * 純函式模組（如 queue_priority.py）—— 決策邏輯抽成可單測的 pure core
  - **不要壓行來過關**：量的是 AST statement 數，把兩行併成一行不會變少。
  - 例外：in-file Extract Method（巨型方法拆出有名字的子方法、行為不變）會讓 method 數 /
    statement 數微升——這是拆解不是加功能，允許據實上修，並同步下修 FROZEN_METHODS。

2026-09-29 從行數改為 AST 量測（見 core_size_metrics.py）：行數預算頂滿 1500/1500 後
出現為過關壓行（48069ba），指標守住、可讀性變差。四個指標：
  1. STATEMENT_BUDGET：整檔 statement 數（import 不算）
  2. METHOD_BUDGET：MusicCog 自身 method 數
  3. SELF_ATTR_BUDGET：檔內被賦值的 self.X 名稱數（共用可變狀態＝耦合）
  4. 單一 method 上限：新 method ≤ NEW_METHOD_MAX；已超標的大 method 凍結在現值只准縮
     （縮了就把 FROZEN_METHODS 的數字改成新實測值；縮到 ≤ NEW_METHOD_MAX 就刪掉那行）

行數演進（舊指標，留作歷史）：Phase 1-8 抽 commands/subsystem/personal_shuffle/audio_meta/
autopilot/story_arc/dj_lyrics/tail_dj 八塊 mixin，4863→1527；2026-09-25 點歌插入位置搬去
queue_priority 後 1500。
"""
from __future__ import annotations

from pathlib import Path

from core_size_metrics import method_statements, self_attrs, statement_count

MC = Path(__file__).resolve().parent.parent / "cogs" / "music_cog.py"
CLASS = "MusicCog"

# ── 棘輪基準（2026-09-29 改 AST 量測時的實測值）──────────────────────────────
STATEMENT_BUDGET = 880
METHOD_BUDGET = 29
SELF_ATTR_BUDGET = 58

NEW_METHOD_MAX = 40
FROZEN_METHODS = {  # 已超過 NEW_METHOD_MAX 的既有大 method：只准縮
    "_handle_voice_music_command": 228,
    "_stream_loop": 76,
    "_resolve_yt_query": 75,
    "_stream_loop_prepare_and_announce": 63,
    "__init__": 55,
}

_HINT = (
    "不要為了塞功能調高預算 —— 新功能請進 IntentAgent / mixin / 純函式模組。\n"
    "若這是把程式碼「移出去」造成的合法下降，請把 budget 改成新的實測值。"
)


def test_music_cog_statement_count_within_budget():
    n = statement_count(MC)
    assert n <= STATEMENT_BUDGET, f"music_cog.py statement 數 {n} > 預算 {STATEMENT_BUDGET}。\n{_HINT}"


def test_music_cog_method_count_within_budget():
    n = len(method_statements(MC, CLASS))
    assert n <= METHOD_BUDGET, f"MusicCog 自身 method 數 {n} > 預算 {METHOD_BUDGET}。\n{_HINT}"


def test_music_cog_self_attr_count_within_budget():
    n = len(self_attrs(MC))
    assert n <= SELF_ATTR_BUDGET, (
        f"music_cog.py 賦值的 self.X 屬性 {n} 個 > 預算 {SELF_ATTR_BUDGET}。\n"
        f"新狀態請放進功能自己的物件/模組，不要再往 MusicCog 的共用 self 上掛。"
    )


def test_music_cog_method_size_within_budget():
    over = {
        name: (n, FROZEN_METHODS.get(name, NEW_METHOD_MAX))
        for name, n in method_statements(MC, CLASS).items()
        if n > FROZEN_METHODS.get(name, NEW_METHOD_MAX)
    }
    assert not over, (
        f"method statement 數超標 {{名稱: (實測, 上限)}}：{over}\n"
        f"大 method 只准縮不准長 —— 新邏輯抽成獨立 method 或純函式模組。"
    )
