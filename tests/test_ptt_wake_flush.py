"""/wake、/flush（PTT）：satellite wyoming 橋拔掉後只剩 mixer ducking 語意。

/wake：設 _ptt_active=True 並觸發 _on_satellite_wake（duck 音樂）。
/flush：解除 _ptt_active，一律回 200（Discord 進程沒有 bridge，舊行為是 400）。
"""
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

import car_http_app


def _vc():
    vc = MagicMock()
    vc._mixer._ptt_active = False
    return vc


@pytest.mark.asyncio
async def test_wake_sets_ptt_and_ducks():
    vc = _vc()
    app = car_http_app.build_text_app(vc, token=None)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/wake")
        assert resp.status == 200
        assert (await resp.json())["ok"] is True
    assert vc._mixer._ptt_active is True
    vc._on_satellite_wake.assert_called_once_with("hey_marvin")


@pytest.mark.asyncio
async def test_flush_clears_ptt_and_returns_ok():
    vc = _vc()
    vc._mixer._ptt_active = True
    app = car_http_app.build_text_app(vc, token=None)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/flush")
        assert resp.status == 200
        assert (await resp.json())["ok"] is True
    assert vc._mixer._ptt_active is False
