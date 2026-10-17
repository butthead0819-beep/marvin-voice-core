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
    from car_http_app import build_text_app
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
    from car_http_app import build_text_app
    app = build_text_app(_make_vc(), token="s3cret")
    async with TestClient(TestServer(app)) as client:
        html = await (await client.get("/hud?t=s3cret")).text()
        assert "s3cret" in html
        assert "__TOKEN__" not in html


@pytest.mark.asyncio
async def test_hud_includes_shared_drawing_and_performance_script():
    """/hud 共用 hud_shared.js 的繪圖函式 + 仍載完整 hud_performance.js 疊加表演；
    /stage 套殼（layout 旗標）已經拔掉，不該再出現在頁面裡。"""
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    app = build_text_app(_make_vc(), token=None)
    async with TestClient(TestServer(app)) as client:
        html = await (await client.get("/hud")).text()
        assert "function createMarvinHead" in html
        assert "function drawVinylDisc" in html
        assert "PERF_ROUND_SEC" in html
        assert "__SHARED_JS__" not in html
        assert "IS_STAGE" not in html
        assert "__LAYOUT__" not in html
        assert "function createGroove" in html
        assert "__GROOVE_JS__" not in html


@pytest.mark.asyncio
async def test_hud_rejects_missing_token():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    app = build_text_app(_make_vc(), token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/hud")
        assert resp.status == 401


@pytest.mark.asyncio
async def test_hud_no_token_configured_allows_access():
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    app = build_text_app(_make_vc(), token=None)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/hud")
        assert resp.status == 200


@pytest.mark.asyncio
async def test_hud_default_keeps_presentation_chrome():
    """沒帶 ?kiosk=1 → 瀏覽器預覽模式，body 不掛 kiosk class，外殼照舊顯示。"""
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    app = build_text_app(_make_vc(), token=None)
    async with TestClient(TestServer(app)) as client:
        html = await (await client.get("/hud")).text()
        assert '<body class="">' in html


@pytest.mark.asyncio
async def test_hud_kiosk_param_strips_chrome_class():
    """?kiosk=1 → body 加 kiosk class，CSS 拿掉外殼、screen 滿版。"""
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    app = build_text_app(_make_vc(), token=None)
    async with TestClient(TestServer(app)) as client:
        html = await (await client.get("/hud?kiosk=1")).text()
        assert '<body class="kiosk">' in html


@pytest.mark.asyncio
async def test_hud_includes_queue_card_markup():
    """待播清單卡片：demo scene 帶 queue markup，前端輪詢 /now 時用真實 queue 覆蓋。"""
    from aiohttp.test_utils import TestClient, TestServer
    from car_http_app import build_text_app
    app = build_text_app(_make_vc(), token=None)
    async with TestClient(TestServer(app)) as client:
        html = await (await client.get("/hud")).text()
        assert "待播清單" in html
        assert "resolveQueue" in html
        assert "qstack" in html
        assert "qnext" in html
        assert "qpeek" in html


def test_template_placeholders_appear_exactly_once():
    """佔位字樣出現在註解裡會被整段程式碼替換進 // 註解行，整頁 JS 語法錯誤、HUD 全黑。"""
    import re
    import car_http_app as c
    for tpl in (c.HUD_HTML, c._STAGE_HTML):
        for p in set(re.findall(r"__[A-Z_]+__", tpl)):
            assert tpl.count(p) == 1, p
    assert re.findall(r"__[A-Z_]+__", c._SHARED_JS) == []
