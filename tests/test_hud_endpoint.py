"""
tests/test_hud_endpoint.py
TDD：GET /hud — Marvin HUD 寬屏顯示頁（Mac 自服務，比照 /satellite 模式）。

v12 設計稿（黑膠現正播放卡＋會動 Marvin 頭）先只是靜態 demo 資料；這裡驗證頁面
確實服務出來、token 有嵌入頁面（頁面自己要用它打 /now）、且走跟其他端點一樣的
統一 token gate（見 test_satellite_token_middleware.py）。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock


def _make_vc():
    vc = MagicMock()
    vc.handle_stt_result = AsyncMock()
    vc.bot.cogs.get.return_value = None
    return vc


@pytest.mark.asyncio
async def test_hud_serves_html_page():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    app = build_text_app(_make_vc(), token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/hud?t=s3cret")
        assert resp.status == 200
        assert "text/html" in resp.headers["Content-Type"]
        html = await resp.text()
        assert "Marvin HUD" in html
        assert "/now" in html          # 輪詢現正播放
        assert "vinyl" in html         # 黑膠卡


@pytest.mark.asyncio
async def test_hud_injects_token_into_page():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    app = build_text_app(_make_vc(), token="s3cret")
    async with TestClient(TestServer(app)) as client:
        html = await (await client.get("/hud?t=s3cret")).text()
        assert "s3cret" in html
        assert "__TOKEN__" not in html


@pytest.mark.asyncio
async def test_hud_rejects_missing_token():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    app = build_text_app(_make_vc(), token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/hud")
        assert resp.status == 401


@pytest.mark.asyncio
async def test_hud_no_token_configured_allows_access():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    app = build_text_app(_make_vc(), token=None)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/hud")
        assert resp.status == 200
