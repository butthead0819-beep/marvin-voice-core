"""漫畫流程 dry-run（新故事導演 render_story 路徑）：
chat_summary_log.txt（日誌骨幹）+ marvin.db（爆笑精華）→ fuse → StoryPlan
→ choose_template → template_rows，印出分格 / 故事 arc / 字幕。

跳過生圖。LLM 潤飾的 beats（場景/字幕細節/標題）若無 quota → 走 fallback（明確標示）。
用法：venv_simon/bin/python scripts/diary_comic_dryrun_story.py
"""
import datetime
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, ".")
from diary_comic.parser import parse_log, dedupe_adjacent, eligible_sessions, DiaryEntry
from diary_comic.highlight import find_highlights, clean_highlight, meme_needs_marvin
from diary_comic.story import fuse, choose_template, TEMPLATE_HEIGHTS
from diary_comic.render import template_rows
from diary_comic_poster import LOG_PATH

# text_fn=None → 無 LLM，全走 fallback（429 時就是這樣）
TEXT_FN = None


def main():
    sessions = eligible_sessions(dedupe_adjacent(parse_log(
        Path(LOG_PATH).read_text(encoding="utf-8"))))
    session = sessions[-1]
    start = session[0].ts_str
    print(f"日誌場次：{start} → {session[-1].ts_str}（{len(session)} 筆）")

    # 對齊場次時間窗，從 marvin.db 抽 highlights
    cut = datetime.datetime.fromisoformat(start).timestamp() - 600
    con = sqlite3.connect("marvin.db")
    rows = con.execute("SELECT speaker,text,timestamp FROM transcripts "
                       "WHERE timestamp>=? ORDER BY timestamp", (cut,)).fetchall()
    con.close()
    highlights = find_highlights(rows)
    print(f"marvin.db 抽到 {len(highlights)} 個爆笑精華\n")

    plan = fuse(session, highlights)
    if plan is None:
        print("fuse → None（沒精華或太薄）→ 不出漫畫")
        return

    print("=" * 66)
    print(f"StoryPlan.format = {plan.format}　needs_marvin={getattr(plan,'needs_marvin',False)}")
    peak = plan.highlight
    print(f"高潮精華：{peak.laugher} 爆笑（強度 {peak.strength}）笑聲「{peak.laugh_text[:14]}」")
    print(f"  鋪哏：{'；'.join(t[:30] for _,t in peak.setup[-2:])}")

    if plan.format == "meme":
        print(f"\n→ 單格 meme（薄場次）　top（鋪哏）：{plan.meme_top}")
        print(f"   bottom：{'馬文救援（LLM 生）' if plan.needs_marvin else '梗本身/留空'}")
        return

    # slant：故事導演排版
    tid = choose_template(plan, day_index=datetime.date.today().toordinal()) or "T1"
    pool = "PUNCHY(衝)" if not meme_needs_marvin(plan.highlight) else "STEADY(穩)"
    print(f"\n版面樣板 = {tid}（{pool} 分層）　列高 {TEMPLATE_HEIGHTS.get(tid)}")

    scene_context = "；".join(e.core for e in plan.context)
    base = plan.peak_setup
    punch = clean_highlight(plan.highlight, generate_fn=TEXT_FN) or "全場爆笑"
    print("（LLM beats=fallback：場景用 base、字幕用原句拼接；有 quota 時這些會被導演潤飾）\n")

    from diary_comic.layout import Panel
    fb_img = None  # 不生圖
    parts = {
        "focus_zoom": Panel(image=fb_img, heat=4, caption=""),
        "wide": Panel(image=fb_img, heat=3, caption=""),
        "setup": Panel(image=fb_img, heat=9, caption=base.core),
        "react": Panel(image=fb_img, heat=11, caption=punch),
    }
    if tid != "T4":
        parts["mid"] = Panel(image=fb_img, heat=5, caption="")
    else:
        parts["after_a"] = Panel(image=fb_img, heat=4, caption="（余韵反應）")
        parts["after_b"] = Panel(image=fb_img, heat=4, caption="")

    role_scene = {
        "focus_zoom": ("格1 焦點", scene_context or "大家聚在一起聊天"),
        "wide": ("格1 全景", scene_context or "大家聚在一起聊天"),
        "setup": ("Hero 上：鋪哏", base.core),
        "react": ("Hero 下：爆笑", "全場哄堂大笑"),
        "mid": ("中景 develop", base.core),
        "after_a": ("余韵A", "笑完之後的反應"),
        "after_b": ("余韵B", "笑完之後的反應"),
    }

    rows_struct = template_rows(tid, parts)
    print("分格（由上到下）：")
    print("=" * 66)
    for ri, row in enumerate(rows_struct, 1):
        kind = row[0]
        panels = [p for p in row[1:] if isinstance(p, Panel)]
        label = {"single": "整列1格", "duo": "斜切2格", "pair": "並排2格"}.get(kind, kind)
        print(f"\n第 {ri} 列（{label}）：")
        for p in panels:
            # 找這個 panel 的 role
            role = next((r for r, pp in parts.items() if pp is p), "?")
            rname, scene = role_scene.get(role, (role, ""))
            print(f"   • {rname}　heat={p.heat}")
            print(f"     場景：{scene}")
            print(f"     字幕：{p.caption or '（空，導演有 quota 時生）'}")
    print("\n" + "=" * 66)
    print("標題：（LLM 生，429 中→空）")


if __name__ == "__main__":
    main()
