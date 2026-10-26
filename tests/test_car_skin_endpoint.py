"""
tests/test_car_skin_endpoint.py
TDD：GET /car_skin — 車機圓屏頭像底圖（240x240 RGB888 原始 bytes）。

比照 tests/test_car_cover_endpoint.py 同一套測試風格，用注入 tmp_path 的 SkinStore
隔離檔案系統（跟 PuckCoverCache 的注入方式一致：build_text_app(..., puck_skin_store=)）。
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from puck_skin import SKIN_BYTES, SkinStore


def _make_vc():
    vc = MagicMock()
    vc.handle_stt_result = AsyncMock()
    vc.bot.cogs.get.return_value = None
    return vc


@pytest.mark.asyncio
async def test_car_skin_no_current_returns_404(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app

    store = SkinStore(tmp_path)
    app = build_text_app(_make_vc(), token=None, puck_skin_store=store)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_skin")
        assert resp.status == 404


@pytest.mark.asyncio
async def test_car_skin_after_put_returns_200_with_bytes_and_etag(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app

    store = SkinStore(tmp_path)
    data = bytes([0x7A]) * SKIN_BYTES
    etag = store.put("winter", data)

    app = build_text_app(_make_vc(), token=None, puck_skin_store=store)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_skin")
        assert resp.status == 200
        assert resp.headers["X-Skin-Etag"] == etag
        body = await resp.read()
        assert len(body) == SKIN_BYTES
        assert body == data
