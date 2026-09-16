"""
🚧 music_cog.py 防胖守門（ratchet / 棘輪）

music_cog.py 是目前全庫最大的檔案（4863 行、128 method），比 voice_controller.py
拆解前還大，且從未裝過守門——可以無限往上長不會被 CI 擋下來。比照
test_voice_controller_size_budget.py 補上同款棘輪，並跟著 music_cog.py 拆解計畫
（cogs/music_cog_*.py mixin）逐階段調降。

規則（重要）：
  - 這兩個 budget 只能「往下調」（抽離程式碼後同步降低數字）。
  - **絕對不要為了塞新功能把 budget 調高。** 新的音樂功能應該去：
      * 新 IntentAgent（intent_agents/*.py）
      * 新 mixin 模組（cogs/music_cog_*.py）—— 與 MusicCog 共用 self 的內聚方法群
    而不是在 MusicCog 上多寫一個 method 或往現有巨型方法塞行數。

調降時機：每次成功抽離一塊 mixin，就把數字改成新的實測值。
"""
from __future__ import annotations

import re
from pathlib import Path

MC = Path(__file__).resolve().parent.parent / "cogs" / "music_cog.py"

# ── 棘輪基準（2026-09-16，拆解前的原始實測值：4863/128）──────────────────────
# Phase 1（music_cog_commands.py，6個slash指令，−264行/−6method）後：4599/122
# Phase 2（music_cog_subsystem.py，8個radio/stream loop方法，−272行/−8method）後：4327/114
# Phase 3（music_cog_personal_shuffle.py，10個個人歌單/卡片/HUD橋接方法，−281行/−10method）後：4046/104
# Phase 4（music_cog_audio_meta.py，7個音訊分析/檔案清理方法，−203行/−7method）後：3843/97
# Phase 5（music_cog_autopilot.py，20個autopilot推薦引擎方法，−508行/−20method）後：3335/77
# Phase 6（music_cog_story_arc.py，故事弧線節目+_auto_recommend，10個方法，−525行/−10method）後：2810/67
# Phase 7（music_cog_dj_lyrics.py，歌詞抓取+DJ播報生成，20個方法+_DJ_TEMPLATES衍生常數，−20method）後：2184/47
# Phase 8（music_cog_tail_dj.py，metadata統籌預取+PuckMixer橋接+DJ尾段串場排程，18個方法，−657行/−18method）後：1527/29
LINE_BUDGET = 1527
METHOD_BUDGET = 29


def test_music_cog_line_count_within_budget():
    n = len(MC.read_text(encoding="utf-8").splitlines())
    assert n <= LINE_BUDGET, (
        f"music_cog.py 漲到 {n} 行 > 預算 {LINE_BUDGET}。\n"
        f"不要為了塞功能調高預算 —— 新功能請進 IntentAgent / 新 mixin 模組。\n"
        f"若這是把程式碼「移出去」造成的合法下降，請把 LINE_BUDGET 改成新的實測值。"
    )


def test_music_cog_method_count_within_budget():
    # 只數直接定義在 music_cog.py 的 method（4-space 縮排），mixin 不算
    src = MC.read_text(encoding="utf-8")
    n = len(re.findall(r"^    (?:async )?def ", src, re.MULTILINE))
    assert n <= METHOD_BUDGET, (
        f"MusicCog 自身 method 數漲到 {n} > 預算 {METHOD_BUDGET}。\n"
        f"新增的音樂功能應該去 IntentAgent / mixin，不要在 MusicCog 上長新 method。"
    )
