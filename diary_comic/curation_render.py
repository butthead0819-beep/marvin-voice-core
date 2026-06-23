"""CurationPlan → 現成 render_story 的薄轉接。

整頁(slant)與單格(meme)的渲染 render_story 都已完成；這裡只把策展結果填進 StoryPlan：
- crosstalk(有高潮) → format="slant"（整頁，搶話對白當 Hero、context 當鋪陳）
- topic(沒高潮)     → format="meme"（單格選題，不硬湊多格）

註：render_story 的導演 prompt 目前是「爆笑 moment」措辭；搶話/話題的語氣微調是後續小改，
不影響版面結構。
"""
from __future__ import annotations

from diary_comic.highlight import Highlight
from diary_comic.parser import DiaryEntry
from diary_comic.story import StoryPlan


def curation_to_story_plan(plan) -> StoryPlan:
    h = plan.hero
    lead = h.speakers[0] if h.speakers else ""
    if plan.source == "crosstalk":
        peak = Highlight(ts=0.0, laugher=lead, laugh_text="",
                         strength=int(round(h.heat * 10)), setup=list(h.lines))
        first = h.lines[0][1] if h.lines else ""
        setup = DiaryEntry(ts_str=h.ts_str, core=first, speakers=list(h.speakers))
        reaction = DiaryEntry(ts_str=h.ts_str, core="全場接力搶話、互不相讓",
                              speakers=list(h.speakers))
        context = [DiaryEntry(ts_str=s.ts_str, core=s.summary, speakers=list(s.speakers))
                   for s in plan.context]
        return StoryPlan(format="slant", highlight=peak, context=context,
                         peak_setup=setup, peak_reaction=reaction)
    # topic → 單格 meme
    topic = h.lines[0][1] if h.lines else ""
    peak = Highlight(ts=0.0, laugher=lead, laugh_text="", strength=int(round(h.heat)),
                     setup=[(("．".join(h.speakers) or "群聊"), topic)])
    return StoryPlan(format="meme", highlight=peak, meme_top=topic[:30])
