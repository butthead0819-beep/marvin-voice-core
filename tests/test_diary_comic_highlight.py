"""精華處理器：從逐字稿找「爆笑時刻」+ 前情笑點。

洞察：一群人同時哈哈笑，前幾句一定是精華。STT 常把哄堂收成一筆超長哈哈哈，
所以實務訊號 = 一筆爆笑（≥N 哈 / 笑死）。
"""
from diary_comic.highlight import is_laugh, laugh_strength, find_highlights


def test_laugh_strength_counts_ha_and_bonus():
    assert laugh_strength("哈哈哈哈哈") == 5
    assert laugh_strength("笑死我了") >= 5           # 關鍵詞加成
    assert laugh_strength("對啊通常都要") == 0


def test_is_laugh_detects_markers():
    assert is_laugh("哈哈哈") and is_laugh("笑死") and is_laugh("太好笑")
    assert not is_laugh("我們來討論喇叭")


def _r(speaker, text, ts):
    return (speaker, text, ts)


def test_find_highlights_picks_big_laugh_with_setup():
    rows = [
        _r("A", "他把球踢進自己球門", 100),
        _r("B", "真的假的", 110),
        _r("C", "哈哈哈哈哈哈哈哈", 115),   # 爆笑（8 哈）
    ]
    hs = find_highlights(rows, min_strength=5)
    assert len(hs) == 1
    h = hs[0]
    assert h.laugher == "C"
    # 前情笑點含內容句、不含笑聲本身
    setup_texts = [t for _s, t in h.setup]
    assert "他把球踢進自己球門" in setup_texts
    assert all(not is_laugh(t) for t in setup_texts)


def test_find_highlights_ignores_weak_laugh():
    rows = [_r("A", "嗯嗯", 10), _r("B", "哈哈", 12)]  # 只 2 哈 < 門檻
    assert find_highlights(rows, min_strength=5) == []


def test_find_highlights_merges_nearby_bursts():
    rows = [
        _r("A", "笑點句", 100),
        _r("B", "哈哈哈哈哈哈", 105),
        _r("C", "哈哈哈哈哈哈哈", 110),   # 5s 後又笑 → 同一個哄堂
    ]
    hs = find_highlights(rows, min_strength=5, merge_window_s=30)
    assert len(hs) == 1  # 合併成一個精華時刻


def test_find_highlights_setup_respects_lookback():
    rows = [
        _r("A", "很久以前的句子", 0),
        _r("B", "剛剛的笑點", 200),
        _r("C", "哈哈哈哈哈哈", 205),
    ]
    h = find_highlights(rows, min_strength=5, lookback_s=60, setup_lines=3)[0]
    texts = [t for _s, t in h.setup]
    assert "剛剛的笑點" in texts
    assert "很久以前的句子" not in texts  # 超過 lookback 不抓


# ---- #2 笑點清理（LLM 修糊掉的 STT）----
from diary_comic.highlight import Highlight, clean_highlight, highlight_to_entry


def _h():
    return Highlight(ts=1718000000.0, laugher="狗與露", laugh_text="哈哈哈哈哈哈", strength=6,
                     setup=[("大肚", "他就是很蠢然後把球踢到自己球門"), ("狗與露", "真的假的")])


def test_clean_highlight_uses_injected_llm():
    line = clean_highlight(_h(), generate_fn=lambda s, u: "  把球踢進自家球門還全場罵傻逼  ")
    assert line == "把球踢進自家球門還全場罵傻逼"


def test_clean_highlight_prompt_carries_setup_and_laugh():
    seen = {}

    def spy(system, user):
        seen["u"] = user
        return "笑點"

    clean_highlight(_h(), generate_fn=spy)
    assert "球門" in seen["u"]  # 笑點前情有進 prompt


def test_clean_highlight_fallback_without_llm():
    out = clean_highlight(_h(), generate_fn=None)
    assert "球門" in out  # 無 LLM → 原始拼接，不硬掰


def test_clean_highlight_swallows_llm_failure():
    def boom(s, u):
        raise RuntimeError("down")
    out = clean_highlight(_h(), generate_fn=boom)
    assert out  # 失敗降級成 fallback，不丟例外


# ---- #1 橋接：精華 → 漫畫 beat（DiaryEntry）----
def test_highlight_to_entry_maps_fields():
    e = highlight_to_entry(_h(), core="把球踢進自家球門")
    assert e.core == "把球踢進自家球門"
    assert "狗與露" in e.speakers and "大肚" in e.speakers  # 參與者都在
    assert e.ts_str.count(":") == 2 and e.ts_str.count("-") == 2  # 標準時間字串


def test_highlight_to_entry_renderable_by_render_session():
    from diary_comic.render import render_session
    from PIL import Image
    hs = [_h(), _h(), _h()]
    session = [highlight_to_entry(h, core=f"笑點{i}") for i, h in enumerate(hs)]
    page, layout, _line = render_session(
        session, img_fn=lambda p, a: Image.new("RGB", (50, 50)), text_fn=lambda s, u: "金句")
    assert isinstance(page, Image.Image)
