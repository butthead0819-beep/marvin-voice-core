"""iTunes 專輯封面解析器測試（TDD）。

resolve_cover 用可注入的 fetch，測試不真連網。驗證：洗字建 term、
高信心採用並換高清、低信心/空結果/失敗一律退回 fallback、關 flag 不打。
"""
import pytest

import itunes_cover


def _result(track, artist, art="https://is1-ssl.mzstatic.com/image/thumb/Music/x/source/100x100bb.jpg"):
    return {"results": [{"trackName": track, "artistName": artist, "artworkUrl100": art}]}


def _capturing_fetch(payload):
    """回一個 fetch，記錄被呼叫時的 term。"""
    calls = []

    async def _fetch(term, **kw):
        calls.append(term)
        return payload

    _fetch.calls = calls
    return _fetch


@pytest.mark.asyncio
async def test_confident_match_returns_hires_itunes_art():
    fetch = _capturing_fetch(_result("七里香", "周杰倫"))
    art = await itunes_cover.resolve_cover("七里香", "周杰倫", fallback="YT", fetch=fetch)
    assert art and art != "YT"
    assert "600x600" in art  # 100x100 → 600x600 高清替換
    assert "100x100" not in art


@pytest.mark.asyncio
async def test_scattered_artists_returns_fallback():
    # iTunes 沒聽懂 → 結果散落多位不同藝人、文字又對不上 → 退回（擋自信地錯）
    fetch = _capturing_fetch({"results": [
        {"trackName": "Bohemian Rhapsody", "artistName": "Queen",
         "artworkUrl100": "https://x/100x100bb.jpg"},
        {"trackName": "Imagine", "artistName": "John Lennon",
         "artworkUrl100": "https://y/100x100bb.jpg"},
        {"trackName": "Yesterday", "artistName": "The Beatles",
         "artworkUrl100": "https://z/100x100bb.jpg"},
    ]})
    art = await itunes_cover.resolve_cover("七里香", "周杰倫", fallback="YT", fetch=fetch)
    assert art == "YT"


@pytest.mark.asyncio
async def test_cross_language_artist_cluster_trusts_itunes():
    # 中文查詢、iTunes 回羅馬拼音（文字對不上）但鎖定同一位藝人 → 信任 iTunes #1
    fetch = _capturing_fetch({"results": [
        {"trackName": "Sunny Day", "artistName": "Jay Chou",
         "artworkUrl100": "https://a/100x100bb.jpg"},
        {"trackName": "Sunny Day (Live)", "artistName": "Jay Chou",
         "artworkUrl100": "https://b/100x100bb.jpg"},
    ]})
    art = await itunes_cover.resolve_cover("晴天", "周杰倫", fallback="YT", fetch=fetch)
    assert art == "https://a/600x600bb.jpg"


@pytest.mark.asyncio
async def test_no_artist_weak_text_returns_fallback():
    # 沒藝人 + 文字對不上 → 太冒險，退回
    fetch = _capturing_fetch(_result("Sunny Day", "Jay Chou"))
    art = await itunes_cover.resolve_cover("晴天", None, fallback="YT", fetch=fetch)
    assert art == "YT"


@pytest.mark.asyncio
async def test_empty_results_returns_fallback():
    fetch = _capturing_fetch({"results": []})
    art = await itunes_cover.resolve_cover("七里香", "周杰倫", fallback="YT", fetch=fetch)
    assert art == "YT"


@pytest.mark.asyncio
async def test_fetch_failure_returns_fallback():
    async def _boom(term, **kw):
        return None  # 逾時/網路錯 → _default_fetch 回 None

    art = await itunes_cover.resolve_cover("七里香", "周杰倫", fallback="YT", fetch=_boom)
    assert art == "YT"


@pytest.mark.asyncio
async def test_disabled_flag_skips_and_returns_fallback(monkeypatch):
    monkeypatch.setenv("MARVIN_ITUNES_COVER", "0")

    async def _must_not_call(term, **kw):
        raise AssertionError("關 flag 時不該打 iTunes")

    art = await itunes_cover.resolve_cover("七里香", "周杰倫", fallback="YT", fetch=_must_not_call)
    assert art == "YT"


@pytest.mark.asyncio
async def test_dirty_title_is_cleaned_before_query():
    fetch = _capturing_fetch(_result("七里香", "周杰倫"))
    await itunes_cover.resolve_cover(
        "周杰倫 - 七里香 【官方MV】Official HD", "周杰倫 official channel", fallback="YT", fetch=fetch
    )
    term = fetch.calls[0]
    assert "七里香" in term
    for junk in ("官方", "Official", "HD", "【", "】"):
        assert junk not in term


@pytest.mark.asyncio
async def test_no_title_returns_fallback():
    async def _must_not_call(term, **kw):
        raise AssertionError("沒標題不該打")

    art = await itunes_cover.resolve_cover("", "周杰倫", fallback="YT", fetch=_must_not_call)
    assert art == "YT"


def test_hi_res_swaps_size():
    url = "https://is1-ssl.mzstatic.com/image/thumb/Music/x/source/100x100bb.jpg"
    out = itunes_cover._hi_res(url, 600)
    assert out == "https://is1-ssl.mzstatic.com/image/thumb/Music/x/source/600x600bb.jpg"
