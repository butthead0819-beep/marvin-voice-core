"""一個場次 → 一頁漫畫（風格自動路由 + 出圖 + 拼版）。demo 與上線 poster 共用。

img_fn / text_fn 注入式：測試給假的、production 給真的（nano-banana / gemini text）。
"""
from __future__ import annotations

from diary_comic.parser import heat_score, reduce_to_topics, choose_style
from diary_comic.camera import shot_for
from diary_comic.punchline import generate_page_punchline
from diary_comic.panel_gen import generate_panel_cached
from diary_comic.layout import (
    Panel, compose_page, compose_page_hero, compose_page_webtoon,
    plan_boxes, nearest_aspect_ratio,
)


def template_rows(template_id, parts):
    """樣板 id + 各格 Panel（parts dict）→ compose_page_hero 的 row 結構。

    parts 角色：focus_zoom/wide(格1)、mid(格2)、setup/react(Hero)、after_a/after_b(T4余韵)。
    配 story.TEMPLATE_HEIGHTS[id] 一起餵 compose_page_hero。
    """
    p = parts
    if template_id == "T2":  # 頂爆倒敘：Hero頂 → 中景 → 全景|焦點
        return [("duo", p["setup"], p["react"]), ("single", p["mid"]),
                ("pair", p["wide"], p["focus_zoom"], 0.68)]
    if template_id == "T3":  # 純方正三拍：遠景 → 中景 → Hero底
        return [("single", p["wide"]), ("single", p["mid"]),
                ("duo", p["setup"], p["react"])]
    if template_id == "T4":  # 中央爆+余韵：遠景 → Hero中 → 反應A|反應B
        return [("single", p["wide"]), ("duo", p["setup"], p["react"]),
                ("pair", p["after_a"], p["after_b"], 0.5)]
    # T1（預設）建勢底爆：焦點|全景 → 中景 → Hero底
    return [("pair", p["focus_zoom"], p["wide"], 0.32), ("single", p["mid"]),
            ("duo", p["setup"], p["react"])]


def render_session(session, *, img_fn=None, text_fn=None, cache_dir=None,
                   page_size=(1080, 1920), variant="nano", force_layout=None):
    """把一個場次畫成一頁。回傳 (PIL.Image, layout, marvin_line)。

    layout 預設自動選（長+連貫→webtoon，否則 slant）；force_layout 可指定
    'slant'/'webtoon'/'stack'。
    """
    layout = force_layout if force_layout and force_layout != "auto" else choose_style(session)
    page_entries = session[:8] if layout == "webtoon" else reduce_to_topics(session, 4)
    n = len(page_entries)
    heats = [heat_score(e) for e in page_entries]
    hero = max(range(n), key=lambda i: heats[i])
    marvin_line = generate_page_punchline([e.core for e in page_entries], generate_fn=text_fn)

    if layout == "slant":
        partner = hero + 1 if hero + 1 < n else hero - 1
        char_idx = {hero, partner}
        aspects = ["16:9"] * n
    elif layout == "webtoon":
        char_idx = set(range(n))
        aspects = ["4:3"] * n
    else:
        char_idx = set(range(n))
        boxes = plan_boxes(heats)
        aspects = [nearest_aspect_ratio(boxes[i], page_size) for i in range(n)]

    panels = []
    for i, e in enumerate(page_entries):
        caption = marvin_line if (i == hero and marvin_line) else e.core
        img = generate_panel_cached(
            e, generate_image_fn=img_fn, aspect=aspects[i], cache_dir=cache_dir,
            variant=variant, shot=shot_for(i, n, is_hero=(i == hero)),
            object_only=(i not in char_idx))
        panels.append(Panel(image=img, heat=heats[i], caption=caption))

    if layout == "slant":
        lo, hi = sorted((hero, partner))
        rows, i = [], 0
        while i < n:
            if i == lo:
                rows.append(("duo", panels[lo], panels[hi]))
                i = lo + 1
            elif i == hi:
                i += 1
            else:
                rows.append(("single", panels[i]))
                i += 1
        page = compose_page_hero(rows, page_size)
    elif layout == "webtoon":
        page = compose_page_webtoon(panels, page_width=page_size[0])
    else:
        page = compose_page(panels, page_size=page_size)
    return page, layout, marvin_line


def render_story(plan, *, img_fn=None, text_fn=None, cache_dir=None,
                 page_size=(1080, 1920), variant="nano", day_index=0):
    """StoryPlan → 漫畫頁。出圖/清理/標題用注入式 img_fn/text_fn（None→佔位/fallback）。

    回 None = 不出。骨架已串好；等 API 額度回來，img_fn/text_fn 餵真的就生效。
    """
    if plan is None:
        return None
    if plan.format == "meme":
        return _render_meme(plan, img_fn, text_fn, cache_dir, variant)
    return _render_slant(plan, img_fn, text_fn, cache_dir, page_size, variant, day_index)


def _render_meme(plan, img_fn, text_fn, cache_dir, variant):
    from diary_comic.highlight import highlight_to_entry
    from diary_comic.layout import compose_meme
    from diary_comic.story import build_meme_prompt, parse_meme_text
    scene = highlight_to_entry(plan.highlight)  # 爆笑場景（角色）
    img = generate_panel_cached(
        scene, generate_image_fn=img_fn, aspect="1:1", cache_dir=cache_dir, variant=variant,
        shot="dynamic comedic reaction shot, the whole group bursting into laughter")
    # 一次呼叫：模板菜單 + slot 框架 → LLM 挑模板填詞（強反差單飛 / 反差中 Marvin 救援）
    top, bottom = plan.meme_top, ""
    if text_fn is not None:
        try:
            t, b = parse_meme_text(text_fn(*build_meme_prompt(
                plan.highlight, with_marvin=plan.needs_marvin)))
            top, bottom = (t or top), b
        except Exception:
            pass
    return compose_meme(img, top=top, bottom=bottom, size=(1080, 1080))


def _render_slant(plan, img_fn, text_fn, cache_dir, page_size, variant, day_index=0):
    """整套：choose_template → 故事導演 beats → template_rows → 手調 heights。

    高潮 = Hero 斜切 duo（heat 高自動主導 ≥40%）；格1 焦點+全景同源裁切；T4 多余韵。
    """
    from diary_comic.parser import DiaryEntry
    from diary_comic.highlight import clean_highlight
    from diary_comic.layout import (Panel, crops_from_source, zoom_wide_specs,
                                    split_lr_specs, compose_page_hero, with_title)
    from diary_comic.story import (choose_template, build_story_prompt, parse_story,
                                   build_title_prompt, TEMPLATE_HEIGHTS)
    tid = choose_template(plan, day_index=day_index) or "T1"

    # 故事導演：短窗 STT + 場景脈絡 → beats（text_fn None → 空殼，走 fallback）
    scene_context = "；".join(e.core for e in plan.context)
    story = {"beats": [], "title": ""}
    if text_fn is not None:
        try:
            story = parse_story(text_fn(*build_story_prompt(plan.highlight, scene_context, tid)))
        except Exception:
            pass
    beats = {b.get("role"): b for b in story.get("beats", []) if isinstance(b, dict)}
    sc = lambda role, fb: (beats.get(role, {}).get("scene") or fb)
    cp = lambda role, fb="": (beats.get(role, {}).get("caption") or fb)

    base = plan.peak_setup

    def gen(scene, aspect, shot):
        e = DiaryEntry(ts_str=base.ts_str, core=scene, speakers=base.speakers)
        return generate_panel_cached(e, generate_image_fn=img_fn, aspect=aspect,
                                     cache_dir=cache_dir, variant=variant, shot=shot)

    punch = cp("punchline", clean_highlight(plan.highlight, generate_fn=text_fn) or "全場爆笑")
    parts = {}
    # 格1 establish 源（2K 為裁切）→ 焦點 + 全景（一張裁兩格）
    estab = gen(sc("establish", scene_context or "大家聚在一起聊天"), "3:2",
                "wide establishing two-shot, one character on the far left, the group on the right")
    parts["focus_zoom"], parts["wide"] = crops_from_source(estab, zoom_wide_specs(
        (0.34, 0.02, 0.66, 0.52), captions=["", cp("establish")], heats=[4, 3]))
    # Hero 斜切 duo：setup 中景 + react 情緒特寫
    parts["setup"] = Panel(image=gen(sc("setup", base.core), "16:9",
        "medium shot, the character delivering the funny line deadpan"),
        heat=9, caption=cp("setup", base.core))
    parts["react"] = Panel(image=gen(sc("punchline", "全場哄堂大笑"), "16:9",
        "dramatic close-up on the whole group bursting out laughing, intense emotion, "
        "exaggerated faces, broken-border energy"), heat=11, caption=punch)
    if tid != "T4":  # 格2 中景（T4 不用，省一張）
        parts["mid"] = Panel(image=gen(sc("develop", base.core), "16:9",
            "medium shot showing the characters' actions and body language"),
            heat=5, caption=cp("develop"))
    else:            # T4 余韵：一張裁左右兩反應
        asrc = gen(sc("aftermath", "笑完之後的余韵反應"), "3:2",
                   "two reaction close-ups, characters still amused")
        parts["after_a"], parts["after_b"] = crops_from_source(asrc, split_lr_specs(
            0.5, captions=[cp("aftermath"), ""], heats=[4, 4]))

    rows = template_rows(tid, parts)
    page = compose_page_hero(rows, page_size, heights=TEMPLATE_HEIGHTS.get(tid))
    title = story.get("title") or ""
    if not title and text_fn is not None:
        try:
            title = (text_fn(*build_title_prompt([scene_context, punch])) or "").strip()
        except Exception:
            title = ""
    return with_title(page, title)
