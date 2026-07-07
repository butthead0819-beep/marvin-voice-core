"""點歌 query→videoId 快取：點播過的歌不再重送 yt-dlp 6s 搜尋。

現有 _yt_resolve_cache 只記 videoId→info（URL 直點才受益）；文字查詢每次都走
ytsearch5。這個 cache 補「正規化 query → webpage_url」那層，命中則改走 URL 解析
（省下 ~6s 的 search）。持久化到 JSON，跨 session 重播也受益。
"""
from music_request_guard import QueryResolveCache


def test_put_get_roundtrip():
    c = QueryResolveCache(path=None)
    c.put("播放陶喆的愛很簡單", "https://www.youtube.com/watch?v=abc", "愛很簡單")
    hit = c.get("播放陶喆的愛很簡單")
    assert hit is not None
    assert hit["webpage_url"].endswith("abc")
    assert hit["title"] == "愛很簡單"


def test_normalizes_key_whitespace_and_case():
    c = QueryResolveCache(path=None)
    c.put("  Tanya 愛很簡單 ", "u", "t")
    # 大小寫 / 前後空白 / 內部空白差異都應命中同一鍵
    assert c.get("tanya愛很簡單") is not None


def test_miss_returns_none():
    c = QueryResolveCache(path=None)
    assert c.get("從沒點過的歌") is None


def test_ignores_empty_key_or_url():
    c = QueryResolveCache(path=None)
    c.put("", "u", "t")            # 空 query 不存
    c.put("q", "", "t")           # 空 url 不存
    assert c.get("") is None
    assert c.get("q") is None


def test_persistence_roundtrip(tmp_path):
    p = str(tmp_path / "qcache.json")
    c1 = QueryResolveCache(path=p)
    c1.put("陶喆愛很簡單", "https://y/watch?v=xyz", "愛很簡單")
    c2 = QueryResolveCache(path=p)   # 重載（模擬重啟）
    hit = c2.get("陶喆愛很簡單")
    assert hit is not None and hit["title"] == "愛很簡單"


import pytest

pytest.importorskip("rapidfuzz")
pytest.importorskip("pypinyin")


def test_fuzzy_matches_garble_tail_drift():
    # 同一首歌尾巴被 STT 糊得不同（慢歌→慢）→ 拼音 fuzzy 命中同一首
    c = QueryResolveCache(path=None)
    c.put("消防器的慢歌", "https://y/watch?v=fire", "滅火器")
    hit = c.get("消防器的慢")
    assert hit is not None and hit["title"] == "滅火器"


def test_fuzzy_matches_homophone_drift():
    c = QueryResolveCache(path=None)
    c.put("消防器的慢歌", "u", "滅火器")
    assert c.get("消防氣的慢歌") is not None   # 器→氣 同音字漂移


def test_fuzzy_rejects_different_and_short():
    c = QueryResolveCache(path=None)
    c.put("消防器的慢歌", "u", "滅火器")
    assert c.get("消防車") is None            # 不同歌
    assert c.get("周杰倫的七里香") is None      # 完全無關
    assert c.get("消防") is None              # 太短，不 fuzzy（易假命中）


def test_exact_takes_precedence_over_fuzzy():
    c = QueryResolveCache(path=None)
    c.put("陶喆的愛很簡單", "https://y/watch?v=exact", "愛很簡單")
    assert c.get("陶喆的愛很簡單")["webpage_url"].endswith("exact")


def test_delete_invalidates_stale_entry():
    # 影片下架 → delete 清掉，get 回 None（防永久走失效 URL）
    c = QueryResolveCache(path=None)
    c.put("陳華的左邊的人", "https://y/watch?v=dead", "左邊的人")
    assert c.get("陳華的左邊的人") is not None
    c.delete("陳華的左邊的人")
    assert c.get("陳華的左邊的人") is None


def test_size_cap_evicts_oldest():
    c = QueryResolveCache(path=None, max_size=2)
    c.put("a", "u1", "t")
    c.put("b", "u2", "t")
    c.put("c", "u3", "t")           # 超過上限 → 淘汰最舊的 a
    assert c.get("a") is None
    assert c.get("c") is not None
