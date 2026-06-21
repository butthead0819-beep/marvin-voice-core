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
