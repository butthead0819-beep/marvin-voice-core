"""
tests/test_stage_endpoint.py
GET /stage — Marvin iPhone 專用頁（直式為主、橫式雙欄，可「加入主畫面」全螢幕）。

/stage 服務 hud_stage.html（跟 /hud 的 HUD_HTML 是不同模板，只共用 hud_shared.js 的繪圖
函式），不載 hud_performance.js（不疊加演出，省電）。這裡只打 HTTP 驗證：模板內容確實
嵌入 token／共用繪圖函式、走統一 token gate；/stage_icon.png 額外放行不需 token。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock


def _make_vc():
    vc = MagicMock()
    vc.handle_stt_result = AsyncMock()
    vc.bot.cogs.get.return_value = None
    return vc


@pytest.mark.asyncio
async def test_stage_serves_iphone_page():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    app = build_text_app(_make_vc(), token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/stage?t=s3cret")
        assert resp.status == 200
        assert "text/html" in resp.headers["Content-Type"]
        html = await resp.text()
        assert "apple-mobile-web-app-capable" in html
        assert "viewport-fit=cover" in html
        assert "s3cret" in html
        assert "function createMarvinHead" in html
        assert "function drawVinylDisc" in html
        assert "/marvin_comment" in html
        assert "__TOKEN__" not in html
        assert "__SHARED_JS__" not in html
        assert "PERF_ROUND_SEC" not in html   # 不載演出
        assert "cqh" not in html


@pytest.mark.asyncio
async def test_stage_rejects_missing_token():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    app = build_text_app(_make_vc(), token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/stage")
        assert resp.status == 401


@pytest.mark.asyncio
async def test_stage_icon_allowed_without_token():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    app = build_text_app(_make_vc(), token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/stage_icon.png")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "image/png"
        body = await resp.read()
        assert body[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.asyncio
async def test_token_gate_not_widened_for_other_endpoints():
    """/stage_icon.png 的特例放行只限它自己——其餘端點仍要驗 token。"""
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    app = build_text_app(_make_vc(), token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/now")
        assert resp.status == 401
