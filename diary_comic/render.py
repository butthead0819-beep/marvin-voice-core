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
                 page_size=(1080, 1920), variant="nano"):
    """StoryPlan → 漫畫頁。出圖/清理/標題用注入式 img_fn/text_fn（None→佔位/fallback）。

    回 None = 不出。骨架已串好；等 API 額度回來，img_fn/text_fn 餵真的就生效。
    """
    if plan is None:
        return None
    if plan.format == "meme":
        return _render_meme(plan, img_fn, text_fn, cache_dir, variant)
    return _render_slant(plan, img_fn, text_fn, cache_dir, page_size, variant)


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


def _render_slant(plan, img_fn, text_fn, cache_dir, page_size, variant):
    """大砸框：高潮(爆笑反應)=底部 ≥40% 砸框，鋪陳(物件context + 鋪哏setup)=上方小格。"""
    from diary_comic.highlight import clean_highlight
    from diary_comic.layout import Panel, compose_splash_page, with_title
    from diary_comic.story import build_title_prompt
    # 鋪陳：物件 context 小格（為高潮服務）
    support = []
    for i, e in enumerate(plan.context):
        img = generate_panel_cached(
            e, generate_image_fn=img_fn, aspect="4:3", cache_dir=cache_dir, variant=variant,
            shot=shot_for(i, len(plan.context) + 2, is_hero=False), object_only=True)
        support.append(Panel(image=img, heat=heat_score(e), caption=e.core))
    # 鋪哏 setup（角色、中景）也是鋪陳格
    setup_img = generate_panel_cached(
        plan.peak_setup, generate_image_fn=img_fn, aspect="4:3", cache_dir=cache_dir,
        variant=variant, shot="medium shot, the character delivering the funny line deadpan")
    support.append(Panel(image=setup_img, heat=9, caption=plan.peak_setup.core))
    # 高潮 = 爆笑反應，情緒特寫，當大砸框
    react_img = generate_panel_cached(
        plan.peak_reaction, generate_image_fn=img_fn, aspect="16:9", cache_dir=cache_dir,
        variant=variant,
        shot="dramatic close-up on the whole group bursting out laughing, intense emotion, "
             "exaggerated faces, broken-border energy")
    punch = clean_highlight(plan.highlight, generate_fn=text_fn)
    climax = Panel(image=react_img, heat=10, caption=punch)

    page = compose_splash_page(support, climax, page_size, climax_frac=0.45)
    title = ""
    if text_fn is not None:
        try:
            title = (text_fn(*build_title_prompt(
                [e.core for e in plan.context] + [punch])) or "").strip()
        except Exception:
            title = ""
    return with_title(page, title)
