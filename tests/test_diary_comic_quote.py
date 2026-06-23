"""今夜馬文語錄：LLM 從當夜主題生 + epigraph 條渲染。"""
from PIL import Image

from diary_comic.parser import DiaryEntry
from diary_comic.layout import compose_quote_strip, prepend_quote
import diary_comic_poster as poster


def _entries(n):
    return [DiaryEntry(ts_str=f"2026-06-22 22:{i*3:02d}:00", core=f"聊主題{i}",
                       speakers=["a", "b"]) for i in range(n)]


def test_gen_marvin_quote_uses_topics_and_strips_quotes():
    seen = {}

    def fake_text(system, user):
        seen["user"] = user
        return "「人類的對話，不過是延緩寂靜的雜音。」"

    q = poster._gen_marvin_quote(_entries(3), fake_text)
    assert q == "人類的對話，不過是延緩寂靜的雜音。"  # 引號被剝掉
    assert "聊主題0" in seen["user"]                  # 主題有進 prompt


def test_gen_marvin_quote_no_textfn_empty():
    assert poster._gen_marvin_quote(_entries(3), None) == ""


def test_gen_marvin_quote_textfn_raises_safe():
    def boom(s, u):
        raise RuntimeError("x")
    assert poster._gen_marvin_quote(_entries(3), boom) == ""


def test_compose_quote_strip_image():
    strip = compose_quote_strip("人類的對話充滿無意義的重複", width=1080)
    assert isinstance(strip, Image.Image) and strip.width == 1080 and strip.height > 60


def test_prepend_quote_grows_or_passthrough():
    page = Image.new("RGB", (1080, 1920))
    assert prepend_quote(page, "") is page              # 空→原圖
    out = prepend_quote(page, "可悲的人類")
    assert out.height > 1920                            # 接了語錄變高
