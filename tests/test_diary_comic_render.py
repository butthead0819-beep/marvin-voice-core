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
