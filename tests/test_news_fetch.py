"""Google News RSS 新聞抓取測試（TDD）。

fetch 可注入，不真連網。驗證：關鍵字帶入查詢、抓到標題並清掉來源後綴、
空/壞 XML/關 flag 一律 fail-open 回 None。
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
    result = await news_fetch.fetch_news_headline("天氣", fetch=fetch)
    assert result == {"title": "台北今日大雨特報"}
    assert fetch.calls == ["天氣"]


@pytest.mark.asyncio
async def test_no_keyword_queries_top_headlines():
    fetch = _capturing_fetch(_rss("頭條新聞 - 某媒體"))
    await news_fetch.fetch_news_headline(fetch=fetch)
    assert fetch.calls == [None]


@pytest.mark.asyncio
async def test_title_without_source_suffix_kept_as_is():
    fetch = _capturing_fetch(_rss("沒有後綴的標題"))
    result = await news_fetch.fetch_news_headline("測試", fetch=fetch)
    assert result == {"title": "沒有後綴的標題"}


@pytest.mark.asyncio
async def test_fetch_failure_returns_none():
    async def _boom(keyword, **kw):
        return None

    result = await news_fetch.fetch_news_headline("測試", fetch=_boom)
    assert result is None


@pytest.mark.asyncio
async def test_malformed_xml_returns_none():
    fetch = _capturing_fetch("<not><valid xml")
    result = await news_fetch.fetch_news_headline("測試", fetch=fetch)
    assert result is None


@pytest.mark.asyncio
async def test_empty_channel_returns_none():
    fetch = _capturing_fetch('<?xml version="1.0"?><rss><channel></channel></rss>')
    result = await news_fetch.fetch_news_headline("測試", fetch=fetch)
    assert result is None


@pytest.mark.asyncio
async def test_disabled_flag_skips_and_returns_none(monkeypatch):
    monkeypatch.setenv("MARVIN_NEWS_BROADCAST", "0")

    async def _must_not_call(keyword, **kw):
        raise AssertionError("關 flag 時不該打 Google News")

    result = await news_fetch.fetch_news_headline("測試", fetch=_must_not_call)
    assert result is None
