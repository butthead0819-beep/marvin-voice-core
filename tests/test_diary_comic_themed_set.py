"""主題歌單 Step 5：日記讀 records/themed_sets.jsonl → 「今夜歌單」卡。"""
import json

from PIL import Image

from diary_comic.themed_set import (parse_themed_sets, latest_themed_set,
                                     ThemedSetRecord)
from diary_comic.layout import compose_themed_set_card, append_themed_set_card


def _line(ts, title, picks):
    return json.dumps({"ts": ts, "theme_title": title, "picks": picks},
                      ensure_ascii=False)


_TEXT = "\n".join([
    _line(1000.0, "溝通卡卡，但總有解方",
          [{"title": "周杰倫 - 開不了口", "reason": "聊到溝通", "url": "https://youtu.be/abcdefghijk"}]),
    _line(2000.0, "深夜的溫柔",
          [{"title": "陶喆 - 普通朋友", "reason": "延續氣氛", "url": ""}]),
    "壞行 not json",
])


def test_parse_filters_window_and_bad_lines():
    sets = parse_themed_sets(_TEXT, since=1500.0, until=2500.0)
    assert len(sets) == 1
    assert sets[0].theme_title == "深夜的溫柔"


def test_parse_skips_record_without_title_or_picks():
    text = "\n".join([
        json.dumps({"ts": 1.0, "theme_title": "", "picks": [{"title": "x"}]}),
        json.dumps({"ts": 2.0, "theme_title": "有題", "picks": []}),
        json.dumps({"ts": 3.0, "theme_title": "好", "picks": [{"title": "歌"}]}),
    ])
    sets = parse_themed_sets(text)
    assert [s.theme_title for s in sets] == ["好"]


def test_latest_returns_last_in_window():
    rec = latest_themed_set(_TEXT)
    assert isinstance(rec, ThemedSetRecord)
    assert rec.theme_title == "深夜的溫柔"  # 一晚多張 → 取最後一張


def test_latest_none_when_empty_window():
    assert latest_themed_set(_TEXT, since=9_000.0) is None


def test_compose_themed_set_card_text_only():
    picks = [{"title": "周杰倫 - 開不了口", "reason": "今晚聊到溝通，這首最對味"},
             {"title": "陶喆 - 普通朋友", "reason": "延續欲言又止"}]
    card = compose_themed_set_card("溝通卡卡，但總有解方", picks)
    assert isinstance(card, Image.Image) and card.width == 1080 and card.height > 100


def test_compose_themed_set_card_with_covers():
    picks = [{"title": "歌一", "reason": "理由一"}, {"title": "歌二", "reason": "理由二"}]
    covers = [Image.new("RGB", (480, 360), (10, 10, 10)), None]
    card = compose_themed_set_card("主題", picks, covers=covers)
    assert isinstance(card, Image.Image)


def test_compose_themed_set_card_pads_short_covers():
    """covers 比 picks 短時要補 None，不可 silently 砍歌（zip(rows, covers) 截斷 bug）。
    補齊後應與顯式給齊長度（[img,None,None]）渲染出完全一致的圖；舊 bug 只畫第一列。"""
    picks = [{"title": f"歌{i}", "reason": f"理由{i}"} for i in range(3)]
    img = Image.new("RGB", (480, 360), (10, 10, 10))
    short = compose_themed_set_card("主題", picks, covers=[img])               # 只給 1 張
    explicit = compose_themed_set_card("主題", picks, covers=[img, None, None])  # 顯式補齊
    assert short.tobytes() == explicit.tobytes()  # 3 首全畫，與顯式等長一致


def test_append_themed_set_card_grows_or_passthrough():
    page = Image.new("RGB", (1080, 1920))
    assert append_themed_set_card(page, "主題", []) is page  # 無歌單→原圖
    out = append_themed_set_card(page, "主題", [{"title": "歌", "reason": "理由"}])
    assert out.height > 1920  # 接了卡片變高
