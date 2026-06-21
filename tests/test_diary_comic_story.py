"""故事編排：日誌(骨幹) + 精華(高潮) → 一頁漫畫故事計畫。

路由（條漫已 off）：有精華才出；豐富→日漫4格(Hero拆兩拍)、薄→一格meme。
"""
from diary_comic.parser import DiaryEntry
from diary_comic.highlight import Highlight
from diary_comic.story import choose_format, fuse, build_title_prompt, StoryPlan


def _diary(n):
    return [DiaryEntry(ts_str=f"2026-06-20 22:{i*5:02d}:00", core=f"討論主題{i}",
                       speakers=["狗與露", "showay"]) for i in range(n)]


def _hl(strength, setup, laugh="哈哈哈哈哈哈"):
    return Highlight(ts=1718000000.0, laugher="狗與露", laugh_text=laugh, strength=strength,
                     setup=[("大肚", s) for s in setup])


def test_choose_format_none_without_highlights():
    assert choose_format(_diary(8), []) is None  # 沒精華不出（B）


def test_choose_format_slant_when_rich():
    assert choose_format(_diary(8), [_hl(8, ["x"])]) == "slant"


def test_choose_format_meme_when_thin():
    assert choose_format(_diary(3), [_hl(8, ["x"])]) == "meme"  # 薄→meme


def test_fuse_slant_has_peak_split_into_two_beats():
    plan = fuse(_diary(8), [_hl(9, ["他把球踢進自家球門"])])
    assert plan.format == "slant"
    assert plan.peak_setup is not None and plan.peak_reaction is not None  # 拆兩拍
    assert plan.context  # 有物件 context 墊


def test_fuse_meme_strong_contrast_no_marvin():
    strong = _hl(11, ["一本正經分析", "把球踢進自家球門"], laugh="哈哈哈哈哈哈哈哈笑死")
    plan = fuse(_diary(2), [strong])
    assert plan.format == "meme"
    assert plan.meme_top  # 有鋪哏
    assert plan.meme_bottom == ""  # 強反差→單飛，沒 Marvin


def test_fuse_meme_mild_contrast_flags_marvin():
    mild = _hl(3, ["還好啦"], laugh="哈哈哈")
    plan = fuse(_diary(2), [mild])
    assert plan.format == "meme"
    assert plan.needs_marvin is True  # 反差中→要 Marvin 救援


def test_build_title_prompt_carries_context():
    s, u = build_title_prompt(["足球烏龍", "擴大機"])
    assert "足球烏龍" in u and s
