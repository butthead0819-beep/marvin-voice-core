"""
tests/test_satellite_token_middleware.py
TDD：統一 token middleware（eng review 架構#1）。

Funnel 公開整台 server → 所有端點都要 token gate，不只 /say /audio。
現況缺口：/now、/satellite 沒 token 檢查（handle_now docstring 明說「無 token 驗證」）。
要求：token 設了 → 全端點無/錯 token 一律 401（含 /now /satellite）；
OPTIONS preflight 不 gate；token=None → 維持 Tailscale 私網現狀（全放行）。

無網路（aiohttp TestServer）、無 Discord、vc 用 MagicMock。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock


def _make_vc():
    vc = MagicMock()
    vc.handle_stt_result = AsyncMock()
    vc.bot.cogs.get.return_value = None   # 關掉 MusicCog，/now 乾淨回 {"playing": False}
    return vc


# ── 缺口：/now 現在沒 gate，設了 token 也該 401 ──────────────────────────────
@pytest.mark.asyncio
async def test_now_rejects_missing_token():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    app = build_text_app(_make_vc(), token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/now")           # 無 token
        assert resp.status == 401


@pytest.mark.asyncio
async def test_now_accepts_valid_token():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    app = build_text_app(_make_vc(), token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/now?t=s3cret")
        assert resp.status == 200


# ── 缺口：/satellite 現在服務網頁不 gate，公開後任何人都拿得到頁 ─────────────
@pytest.mark.asyncio
async def test_satellite_page_rejects_missing_token():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    app = build_text_app(_make_vc(), token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/satellite")     # 無 token
        assert resp.status == 401


@pytest.mark.asyncio
async def test_satellite_page_accepts_valid_token():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    app = build_text_app(_make_vc(), token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/satellite?t=s3cret")
        assert resp.status == 200


# ── 回歸：既有已 gate 的端點 refactor 後仍 401 ──────────────────────────────
@pytest.mark.asyncio
async def test_say_still_rejects_missing_token():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    app = build_text_app(_make_vc(), token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/say", data="哈囉")   # 無 token
        assert resp.status == 401


@pytest.mark.asyncio
async def test_reply_still_rejects_missing_token():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    app = build_text_app(_make_vc(), token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/reply")               # 無 token
        assert resp.status == 401


# ── preflight：OPTIONS 不能被 token gate（CORS preflight 帶不了自訂 auth）──────
@pytest.mark.asyncio
async def test_options_preflight_not_gated():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    app = build_text_app(_make_vc(), token="s3cret")
    async with TestClient(TestServer(app)) as client:
        resp = await client.options("/say")             # 無 token
        assert resp.status == 204


# ── 向後相容：token=None（Tailscale 私網信任）→ 不 gate，全放行 ─────────────
@pytest.mark.asyncio
async def test_no_token_configured_allows_all():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    app = build_text_app(_make_vc(), token=None)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/now")).status == 200
        assert (await client.get("/satellite")).status == 200
