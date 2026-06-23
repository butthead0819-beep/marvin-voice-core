"""CurationPlan → 現成 render_story 的薄轉接：crosstalk→整頁(slant)、topic→單格(meme)。"""
from PIL import Image

from diary_comic.curator import CurationPlan, HeroMoment, Segment
from diary_comic.curation_render import curation_to_story_plan
from diary_comic.render import render_story


def _img(prompt, aspect=None):
    return Image.new("RGB", (200, 200), (180, 150, 120))


def _txt(system, user):
    return '{"understanding":"u","title":"今晚精華","beats":[]}'


def _crosstalk_plan():
    hero = HeroMoment(kind="crosstalk", ts_str="2026-06-22 23:30:50",
                      speakers=["狗與露", "showay", "陳進文"],
                      lines=[("狗與露", "風吹大真的很辛苦啦雨大還好啦"),
                             ("showay", "他有風大真的很辛苦吧"),
                             ("陳進文", "應該是不至於這樣")], heat=3.11)
    ctx = [Segment("2026-06-22 22:00:00", "台中包棟民宿聚會", ["狗與露", "showay"]),
           Segment("2026-06-22 22:30:00", "樂器保養與音樂班", ["大肚", "showay"])]
    return CurationPlan(date="2026-06-22 23:46:00", cast=["狗與露", "showay", "陳進文", "大肚"],
                        hero=hero, context=ctx, source="crosstalk")


def _topic_plan():
    hero = HeroMoment(kind="topic", ts_str="2026-06-15 00:00:03",
                      speakers=["狗與露", "showay"],
                      lines=[("狗與露．showay", "討論 F1 車手積分差距與 NBA 小球時代")], heat=10.0)
    ctx = [Segment("2026-06-15 22:00:00", "AI 渲染立面圖", ["showay"])]
    return CurationPlan(date="2026-06-15 00:16:00", cast=["狗與露", "showay"],
                        hero=hero, context=ctx, source="topic")


def test_crosstalk_maps_to_slant_fullpage():
    sp = curation_to_story_plan(_crosstalk_plan())
    assert sp.format == "slant"
    assert sp.highlight.setup  # 搶話對白進 setup
    page = render_story(sp, img_fn=_img, text_fn=_txt)
    assert isinstance(page, Image.Image)
    assert page.height > page.width  # 直式整頁


def test_topic_maps_to_meme_singleframe():
    sp = curation_to_story_plan(_topic_plan())
    assert sp.format == "meme"
    page = render_story(sp, img_fn=_img, text_fn=_txt)
    assert isinstance(page, Image.Image)
    assert page.size == (1080, 1080)  # 單格方形


def test_topic_meme_top_carries_topic():
    sp = curation_to_story_plan(_topic_plan())
    assert "F1" in sp.meme_top
