"""render_session：場次 → 一頁漫畫（注入式 img_fn/text_fn，可純測）。"""
from PIL import Image

from diary_comic.parser import DiaryEntry
from diary_comic.render import render_session


def _s(ts_min, core):
    return DiaryEntry(ts_str=f"2026-06-20 22:{ts_min:02d}:00", core=core, speakers=["狗與露", "showay"])


def _fake_img(prompt, aspect):
    return Image.new("RGB", (200, 200), (180, 150, 120))


def _fake_text(system, user):
    return "馬文金句測試"


def test_render_session_short_returns_image_and_slant():
    session = [_s(0, "聊喇叭"), _s(10, "聊泡麵"), _s(20, "聊PS4")]  # 短、散 → 日漫
    page, layout, line = render_session(session, img_fn=_fake_img, text_fn=_fake_text)
    assert isinstance(page, Image.Image)
    assert layout == "slant"
    assert line == "馬文金句測試"


def test_render_session_long_coherent_is_webtoon():
    session = [_s(i * 5, f"持續討論音響系統的調校細節第{i}段") for i in range(8)]
    page, layout, _line = render_session(session, img_fn=_fake_img, text_fn=_fake_text)
    assert layout == "webtoon"
    assert page.width == 1080  # 條漫滿寬


def test_render_session_without_text_fn_has_no_punchline():
    session = [_s(0, "聊喇叭"), _s(10, "聊泡麵"), _s(20, "聊PS4")]
    page, _layout, line = render_session(session, img_fn=_fake_img, text_fn=None)
    assert isinstance(page, Image.Image)
    assert line == ""  # 沒 text_fn → 不硬掰金句


# ---- render_story 骨架：StoryPlan → 漫畫頁（注入式 img/text）----
from diary_comic.story import fuse
from diary_comic.highlight import Highlight
from diary_comic.layout import with_title


def _diary2(n):
    return [DiaryEntry(ts_str=f"2026-06-20 22:{i*5:02d}:00", core=f"聊主題{i}",
                       speakers=["狗與露", "showay"]) for i in range(n)]


def _hl2(strength, setup):
    return Highlight(ts=1718000000.0, laugher="狗與露", laugh_text="哈哈哈哈哈哈",
                     strength=strength, setup=[("大肚", s) for s in setup])


def test_with_title_makes_taller_image():
    page = Image.new("RGB", (1080, 1920))
    out = with_title(page, "今晚精華：足球烏龍")
    assert out.width == 1080 and out.height > 1920  # 多了標題 bar


def test_with_title_empty_returns_same():
    page = Image.new("RGB", (1080, 1920))
    assert with_title(page, "").size == (1080, 1920)


def test_render_story_meme_returns_image():
    from diary_comic.render import render_story
    plan = fuse(_diary2(2), [_hl2(11, ["一本正經分析", "把球踢進自家球門"])])
    assert plan.format == "meme"
    page = render_story(plan, img_fn=_fake_img, text_fn=_fake_text)
    assert isinstance(page, Image.Image)


def test_render_story_slant_returns_image_with_title():
    from diary_comic.render import render_story
    plan = fuse(_diary2(8), [_hl2(9, ["他把球踢進自家球門"])])
    assert plan.format == "slant"
    page = render_story(plan, img_fn=_fake_img, text_fn=_fake_text)
    assert isinstance(page, Image.Image)


def test_render_story_none_plan_returns_none():
    from diary_comic.render import render_story
    assert render_story(None, img_fn=_fake_img, text_fn=_fake_text) is None


# ---- template_rows：樣板 id + 各格 Panel → row 結構 ----
from diary_comic.render import template_rows
from diary_comic.layout import Panel as _Panel


def _parts():
    mk = lambda c: _Panel(image=Image.new("RGB", (40, 40)), heat=3, caption=c)
    return {"focus_zoom": mk("焦點"), "wide": mk("全景"), "mid": mk("中景"),
            "setup": mk("鋪哏"), "react": mk("爆笑"),
            "after_a": mk("反應A"), "after_b": mk("反應B")}


def test_template_rows_t1_pair_then_duo():
    rows = template_rows("T1", _parts())
    assert rows[0][0] == "pair" and rows[-1][0] == "duo"  # 焦點+全景 → … → Hero底


def test_template_rows_t2_hero_on_top():
    rows = template_rows("T2", _parts())
    assert rows[0][0] == "duo"  # 頂爆


def test_template_rows_t3_all_single_then_duo():
    rows = template_rows("T3", _parts())
    assert rows[0][0] == "single" and rows[1][0] == "single" and rows[2][0] == "duo"


def test_template_rows_t4_aftermath_pair_last():
    rows = template_rows("T4", _parts())
    assert rows[1][0] == "duo" and rows[-1][0] == "pair"  # 中央爆 + 底部余韵並排


def test_template_rows_every_template_includes_hero_duo():
    for tid in ("T1", "T2", "T3", "T4"):
        rows = template_rows(tid, _parts())
        assert any(r[0] == "duo" for r in rows)  # 每版都有 Hero


def test_render_story_slant_rotates_templates_without_error():
    from diary_comic.render import render_story
    plan = fuse(_diary2(8), [_hl2(11, ["一本正經分析", "把球踢進自家球門"])])  # strong→punchy
    assert plan.format == "slant"
    for di in range(2):  # day0/day1 → 不同樣板都要能出
        page = render_story(plan, img_fn=_fake_img, text_fn=_fake_text, day_index=di)
        assert isinstance(page, Image.Image)


def test_render_story_slant_no_text_fn_still_renders():
    from diary_comic.render import render_story
    plan = fuse(_diary2(8), [_hl2(9, ["他把球踢進自家球門"])])
    page = render_story(plan, img_fn=_fake_img, text_fn=None)  # 無故事 → fallback
    assert isinstance(page, Image.Image)
