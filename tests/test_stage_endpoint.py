"""
tests/test_stage_endpoint.py
GET /stage — Marvin 橫置雙欄頁（左 Marvin 頭、右黑膠）。

/stage 跟 /hud 服務同一份 HUD_HTML，只差注入的 layout 旗標與 body class。
這裡只打 HTTP 驗證：/stage 注入 layout=stage 並嵌入 token、走統一 token gate；
/hud（含 ?kiosk=1）維持原本空 layout，不受影響。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock


def _make_vc():
    vc = MagicMock()
    vc.handle_stt_result = AsyncMock()
    vc.bot.cogs.get.return_value = None
    return vc


@pytest.mark.asyncio
async def test_stage_serves_stage_layout_page():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    app = build_text_app(_make_vc(), token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/stage?t=s3cret")
        assert resp.status == 200
        assert "text/html" in resp.headers["Content-Type"]
        html = await resp.text()
        assert 'const LAYOUT="stage";' in html
        assert '<body class="kiosk stage">' in html
        assert '"/now?t="' in html
        assert "s3cret" in html
        assert "__LAYOUT__" not in html
        assert "PERF_ROUND_SEC" in html   # hud_performance.js 有整段嵌入（它的註解本身含佔位字樣，不能用 not in 驗）
        assert "__TOKEN__" not in html


@pytest.mark.asyncio
async def test_stage_rejects_missing_token():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    app = build_text_app(_make_vc(), token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/stage")
        assert resp.status == 401


@pytest.mark.asyncio
async def test_hud_keeps_default_layout():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    app = build_text_app(_make_vc(), token=None)
    async with TestClient(TestServer(app)) as client:
        html = await (await client.get("/hud")).text()
        assert 'const LAYOUT="";' in html
        assert '<body class="">' in html
        assert "__LAYOUT__" not in html


@pytest.mark.asyncio
async def test_hud_kiosk_keeps_default_layout():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    app = build_text_app(_make_vc(), token=None)
    async with TestClient(TestServer(app)) as client:
        html = await (await client.get("/hud?kiosk=1")).text()
        assert 'const LAYOUT="";' in html
        assert '<body class="kiosk">' in html
