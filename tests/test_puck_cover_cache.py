"""TDD：puck_cover_cache — 車機封面預算快取（單飛 + LRU + 失敗記住）。"""
import asyncio
import logging

import pytest

from puck_cover_cache import PuckCoverCache, cover_url_for


def _info(url, itunes=True):
    info = {"title": "晴天", "thumbnail": url}
    if itunes:
        info["yt_thumbnail"] = "https://yt/x.jpg"
    return info


def _make(max_items=4, max_failed=50):
    calls = {"fetch": 0}

    async def fetch(url):
        calls["fetch"] += 1
        return b"DATA:" + url.encode()

    def render(data, size):
        return b"JPG:" + data + str(size).encode()

    cache = PuckCoverCache(fetch=fetch, render=render, max_items=max_items, max_failed=max_failed)
    return cache, calls


@pytest.mark.asyncio
async def test_non_itunes_cover_returns_none_without_fetch():
    cache, calls = _make()
    result = await cache.get(_info("https://img/a.jpg", itunes=False))
    assert result is None
    assert calls["fetch"] == 0

    result2 = await cache.get(None)
    assert result2 is None
    assert calls["fetch"] == 0


@pytest.mark.asyncio
async def test_itunes_cover_renders_and_caches():
    cache, calls = _make()
    result = await cache.get(_info("https://img/a.jpg"))
    assert result == b"JPG:DATA:https://img/a.jpg466"
    assert calls["fetch"] == 1

    result2 = await cache.get(_info("https://img/a.jpg"))
    assert result2 == result
    assert calls["fetch"] == 1


@pytest.mark.asyncio
async def test_concurrent_requests_single_flight():
    calls = {"fetch": 0}

    async def fetch(url):
        calls["fetch"] += 1
        await asyncio.sleep(0.01)
        return b"DATA:" + url.encode()

    def render(data, size):
        return b"JPG:" + data + str(size).encode()

    cache = PuckCoverCache(fetch=fetch, render=render)
    info = _info("https://img/a.jpg")
    results = await asyncio.gather(cache.get(info), cache.get(info), cache.get(info))
    assert results[0] == results[1] == results[2]
    assert calls["fetch"] == 1


@pytest.mark.asyncio
async def test_lru_eviction():
    cache, calls = _make(max_items=2)
    await cache.get(_info("https://img/a.jpg"))
    await cache.get(_info("https://img/b.jpg"))
    await cache.get(_info("https://img/c.jpg"))
    assert calls["fetch"] == 3

    await cache.get(_info("https://img/a.jpg"))
    assert calls["fetch"] == 4

    await cache.get(_info("https://img/c.jpg"))
    assert calls["fetch"] == 4


@pytest.mark.asyncio
async def test_fetch_failure_remembered():
    calls = {"fetch": 0}

    async def fetch(url):
        calls["fetch"] += 1
        raise RuntimeError("boom")

    cache = PuckCoverCache(fetch=fetch, render=lambda data, size: data)
    result = await cache.get(_info("https://img/a.jpg"))
    assert result is None
    assert calls["fetch"] == 1

    result2 = await cache.get(_info("https://img/a.jpg"))
    assert result2 is None
    assert calls["fetch"] == 1


@pytest.mark.asyncio
async def test_render_failure_returns_none():
    async def fetch(url):
        return b"DATA"

    def render(data, size):
        raise ValueError("bad image")

    cache = PuckCoverCache(fetch=fetch, render=render)
    result = await cache.get(_info("https://img/a.jpg"))
    assert result is None


@pytest.mark.asyncio
async def test_size_is_part_of_cache_key():
    cache, calls = _make()
    info = _info("https://img/a.jpg")
    r240 = await cache.get(info, size=240)
    r466 = await cache.get(info, size=466)
    assert calls["fetch"] == 2
    assert r240 != r466

    r240_again = await cache.get(info, size=240)
    assert r240_again == r240
    assert calls["fetch"] == 2


@pytest.mark.asyncio
async def test_logging_success_and_failure(caplog):
    cache, calls = _make()
    with caplog.at_level(logging.INFO, logger="puck_cover_cache"):
        await cache.get(_info("https://img/a.jpg"))
    assert any("🔘 [PuckCover] 生成" in r.message for r in caplog.records)

    async def fail_fetch(url):
        raise RuntimeError("boom")

    fail_cache = PuckCoverCache(fetch=fail_fetch, render=lambda data, size: data)
    with caplog.at_level(logging.WARNING, logger="puck_cover_cache"):
        await fail_cache.get(_info("https://img/b.jpg"))
    assert any("⚠️ [PuckCover] 失敗" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_lru_hit_refreshes_recency():
    # a、b 進快取 → 命中 a（a 變最新）→ 放 c 時該被踢的是 b，不是 a
    cache, calls = _make(max_items=2)
    await cache.get(_info("https://img/a.jpg"))
    await cache.get(_info("https://img/b.jpg"))
    await cache.get(_info("https://img/a.jpg"))
    await cache.get(_info("https://img/c.jpg"))
    assert calls["fetch"] == 3
    await cache.get(_info("https://img/a.jpg"))
    assert calls["fetch"] == 3
    await cache.get(_info("https://img/b.jpg"))
    assert calls["fetch"] == 4
