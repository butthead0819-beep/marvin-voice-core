"""Google News RSS 新聞抓取測試（TDD）。

fetch 可注入，不真連網。驗證：關鍵字帶入查詢、抓到標題並清掉來源後綴、
最多回 limit 則、不安全標題被跳過、重複標題只留一個，
空/壞 XML/關 flag 一律 fail-open 回 []。
"""
import pytest

import news_fetch


def _rss(*titles):
    items = "".join(f"<item><title>{t}</title></item>" for t in titles)
    return f'<?xml version="1.0"?><rss><channel>{items}</channel></rss>'


def _capturing_fetch(payload):
    calls = []

    async def _fetch(keyword, **kw):
        calls.append(keyword)
        return payload

    _fetch.calls = calls
    return _fetch


@pytest.mark.asyncio
async def test_returns_first_headline_with_source_suffix_stripped():
    fetch = _capturing_fetch(_rss("台北今日大雨特報 - 中央氣象署", "第二則 - 某媒體"))
    result = await news_fetch.fetch_news_headlines("天氣", fetch=fetch)
    assert result == ["台北今日大雨特報", "第二則"]
    assert fetch.calls == ["天氣"]


@pytest.mark.asyncio
async def test_no_keyword_returns_empty_without_fetching():
    async def _must_not_call(keyword, **kw):
        raise AssertionError("沒有 keyword 不該打 Google News")

    assert await news_fetch.fetch_news_headlines(fetch=_must_not_call) == []
    assert await news_fetch.fetch_news_headlines("", fetch=_must_not_call) == []


@pytest.mark.asyncio
async def test_title_without_source_suffix_kept_as_is():
    fetch = _capturing_fetch(_rss("沒有後綴的標題"))
    result = await news_fetch.fetch_news_headlines("測試", fetch=fetch)
    assert result == ["沒有後綴的標題"]


@pytest.mark.asyncio
async def test_fetch_failure_returns_empty_list():
    async def _boom(keyword, **kw):
        return None

    result = await news_fetch.fetch_news_headlines("測試", fetch=_boom)
    assert result == []


@pytest.mark.asyncio
async def test_malformed_xml_returns_empty_list():
    fetch = _capturing_fetch("<not><valid xml")
    result = await news_fetch.fetch_news_headlines("測試", fetch=fetch)
    assert result == []


@pytest.mark.asyncio
async def test_empty_channel_returns_empty_list():
    fetch = _capturing_fetch('<?xml version="1.0"?><rss><channel></channel></rss>')
    result = await news_fetch.fetch_news_headlines("測試", fetch=fetch)
    assert result == []


@pytest.mark.asyncio
async def test_disabled_flag_skips_and_returns_empty_list(monkeypatch):
    monkeypatch.setenv("MARVIN_NEWS_BROADCAST", "0")

    async def _must_not_call(keyword, **kw):
        raise AssertionError("關 flag 時不該打 Google News")

    result = await news_fetch.fetch_news_headlines("測試", fetch=_must_not_call)
    assert result == []


@pytest.mark.asyncio
async def test_skips_unsafe_news_and_keeps_safe_news():
    """遇到車禍、死亡、政治新聞時應自動跳過，保留安全新聞。"""
    fetch = _capturing_fetch(
        _rss(
            "重大車禍造成3人受傷 - 某新聞",
            "立委針對政治議題激烈交鋒 - 某政治報",
            "台積電新廠宣布完工投產 - 科技新報",
        )
    )
    result = await news_fetch.fetch_news_headlines("科技", fetch=fetch)
    assert result == ["台積電新廠宣布完工投產"]


@pytest.mark.asyncio
async def test_returns_empty_list_if_all_news_are_unsafe():
    """若全部新聞皆含有負面/政治/社會事件關鍵字，應回傳 []。"""
    fetch = _capturing_fetch(
        _rss(
            "某地發生墜樓身亡意外 - 社會頭條",
            "政黨候選人捲入貪污醜聞 - 政治快訊",
        )
    )
    result = await news_fetch.fetch_news_headlines("新聞", fetch=fetch)
    assert result == []


@pytest.mark.asyncio
async def test_returns_at_most_limit_headlines():
    fetch = _capturing_fetch(_rss(*[f"第{i}則" for i in range(1, 8)]))
    result = await news_fetch.fetch_news_headlines("測試", fetch=fetch, limit=5)
    assert result == ["第1則", "第2則", "第3則", "第4則", "第5則"]


@pytest.mark.asyncio
async def test_unsafe_title_skipped_but_later_safe_titles_still_returned():
    fetch = _capturing_fetch(
        _rss(
            "命案現場死傷慘重 - 某新聞",
            "台積電新廠宣布完工投產 - 科技新報",
            "第二則安全新聞",
        )
    )
    result = await news_fetch.fetch_news_headlines("測試", fetch=fetch)
    assert result == ["台積電新廠宣布完工投產", "第二則安全新聞"]


@pytest.mark.asyncio
async def test_duplicate_titles_after_suffix_strip_deduped():
    fetch = _capturing_fetch(
        _rss(
            "同一則新聞 - 媒體A",
            "同一則新聞 - 媒體B",
            "第二則",
        )
    )
    result = await news_fetch.fetch_news_headlines("測試", fetch=fetch)
    assert result == ["同一則新聞", "第二則"]


def test_news_url_with_keyword_searches_keyword():
    url = news_fetch.news_url("露營")
    assert "/search?q=%E9%9C%B2%E7%87%9F" in url


# ── pick_news_interest：在場興趣平均抽 ──────────────────────────────────────

class _FirstChoiceRng:
    """假 rng：choice 永遠回傳第一個元素，順便記錄被傳入的候選 list。"""

    def __init__(self):
        self.seen_candidates = None

    def choice(self, candidates):
        self.seen_candidates = list(candidates)
        return candidates[0]


def test_pick_news_interest_strips_喜歡_prefix():
    rng = _FirstChoiceRng()
    result = news_fetch.pick_news_interest(["showay喜歡露營車"], rng=rng)
    assert result == ("showay喜歡露營車", "露營車")


def test_pick_news_interest_empty_interests_returns_none():
    rng = _FirstChoiceRng()
    result = news_fetch.pick_news_interest([], rng=rng)
    assert result is None


def test_pick_news_interest_candidates_dedup_by_keyword():
    rng = _FirstChoiceRng()
    news_fetch.pick_news_interest(
        ["showay喜歡露營車", "狗與露喜歡PS5", "weakgogo喜歡露營車", "爵士樂"],
        rng=rng,
    )
    assert rng.seen_candidates == [
        ("showay喜歡露營車", "露營車"),
        ("狗與露喜歡PS5", "PS5"),
        ("爵士樂", "爵士樂"),
    ]
