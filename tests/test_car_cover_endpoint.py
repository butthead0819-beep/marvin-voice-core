"""
tests/test_car_cover_endpoint.py
TDD：GET /car_cover — 車機圓屏徽章封面 JPEG + 背景預取迴圈 + WROOM cover_sum log。

比照 tests/test_claude_status_endpoint.py 同一套測試風格。
"""
import types

import pytest
from unittest.mock import AsyncMock, MagicMock


class _FakeCache:
    def __init__(self):
        self.calls = []

    async def get(self, info, size=466):
        self.calls.append((info, size))
        if isinstance(info, dict) and info.get("ok"):
            return b"\xff\xd8JPG"
        return None


class _RaisingCache:
    async def get(self, info, size=466):
        raise RuntimeError("boom")


def _make_vc(music_cog=None):
    vc = MagicMock()
    vc.handle_stt_result = AsyncMock()
    vc.bot.cogs.get.return_value = music_cog
    return vc


@pytest.mark.asyncio
async def test_car_cover_current_returns_jpeg():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app

    cache = _FakeCache()
    current_info = {"ok": True, "title": "x"}
    music_cog = types.SimpleNamespace(_current_stream_info=current_info, stream_queue=[])
    app = build_text_app(_make_vc(music_cog), token=None, puck_cover_cache=cache)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_cover")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "image/jpeg"
        body = await resp.read()
        assert body == b"\xff\xd8JPG"

    assert cache.calls[0][0] is current_info


@pytest.mark.asyncio
async def test_car_cover_which_next_and_size():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app

    cache = _FakeCache()
    current_info = {"ok": True, "title": "cur"}
    next_info = {"ok": True, "title": "next"}
    music_cog = types.SimpleNamespace(_current_stream_info=current_info, stream_queue=[next_info])
    app = build_text_app(_make_vc(music_cog), token=None, puck_cover_cache=cache)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_cover?which=next")
        assert resp.status == 200
        assert cache.calls[-1] == (next_info, 466)

        resp = await client.get("/car_cover?size=240")
        assert resp.status == 200
        assert cache.calls[-1] == (current_info, 240)

        resp = await client.get("/car_cover?size=999")
        assert resp.status == 200
        assert cache.calls[-1] == (current_info, 466)


@pytest.mark.asyncio
async def test_car_cover_cache_miss_returns_404():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app

    cache = _FakeCache()
    current_info = {"title": "no itunes cover"}
    music_cog = types.SimpleNamespace(_current_stream_info=current_info, stream_queue=[])
    app = build_text_app(_make_vc(music_cog), token=None, puck_cover_cache=cache)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_cover")
        assert resp.status == 404


@pytest.mark.asyncio
async def test_car_cover_no_music_cog_returns_404_without_cache_call():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app

    cache = _FakeCache()
    app = build_text_app(_make_vc(None), token=None, puck_cover_cache=cache)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_cover")
        assert resp.status == 404
    assert cache.calls == []


@pytest.mark.asyncio
async def test_car_cover_next_empty_queue_returns_404():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app

    cache = _FakeCache()
    music_cog = types.SimpleNamespace(_current_stream_info={"ok": True}, stream_queue=[])
    app = build_text_app(_make_vc(music_cog), token=None, puck_cover_cache=cache)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_cover?which=next")
        assert resp.status == 404


@pytest.mark.asyncio
async def test_prefetch_loop_fetches_current_and_next_both_sizes():
    from car_http_app import run_puck_cover_prefetch_loop

    cache = _FakeCache()
    current_info = {"ok": True, "title": "cur"}
    next_info = {"ok": True, "title": "next"}
    music_cog = types.SimpleNamespace(_current_stream_info=current_info, stream_queue=[next_info])
    vc = _make_vc(music_cog)

    await run_puck_cover_prefetch_loop(vc, cache, interval=0, max_loops=1)

    assert cache.calls == [
        (current_info, 240),
        (current_info, 466),
        (next_info, 240),
        (next_info, 466),
    ]


@pytest.mark.asyncio
async def test_prefetch_loop_no_music_cog_does_not_call_or_raise():
    from car_http_app import run_puck_cover_prefetch_loop

    cache = _FakeCache()
    vc = _make_vc(None)

    await run_puck_cover_prefetch_loop(vc, cache, interval=0, max_loops=1)

    assert cache.calls == []


@pytest.mark.asyncio
async def test_prefetch_loop_swallows_cache_exceptions():
    from car_http_app import run_puck_cover_prefetch_loop

    cache = _RaisingCache()
    music_cog = types.SimpleNamespace(_current_stream_info={"ok": True}, stream_queue=[])
    vc = _make_vc(music_cog)

    await run_puck_cover_prefetch_loop(vc, cache, interval=0, max_loops=2)


@pytest.mark.asyncio
async def test_car_post_logs_cover_sum(caplog):
    import logging
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    from car_presence import CarPresence

    cp = CarPresence(on_arrive=AsyncMock(), on_depart=AsyncMock())
    app = build_text_app(_make_vc(None), token=None, car_presence=cp)
    async with TestClient(TestServer(app)) as client:
        with caplog.at_level(logging.INFO, logger="car_http_app"):
            resp = await client.post(
                "/car", json={"state": "present", "cover_sum": "ok=3 fail=0"})
            assert resp.status == 200

    assert any(
        "🔘 [PuckCover/S3]" in r.message and "ok=3 fail=0" in r.message
        for r in caplog.records
    )
