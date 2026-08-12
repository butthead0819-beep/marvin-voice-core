"""
tests/test_car_control_endpoint.py

TDD：GET /car_control — 人手動下指令給 ESP32 car puck（寫進 puck_command_queue，
ESP32 下次輪詢 /car_commands 才會撿到，見 main_satellite.py::handle_car_control）。

驗：
(a) puck_command_queue=None（功能未開）→ 404
(b) ?cmd=play&url=... → 200，指令真的進了佇列
(c) ?cmd=play 缺 url → 400
(d) ?cmd=stop → 200，指令真的進了佇列
(e) ?cmd=bad → 400
(f) 也吃既有 token middleware：無 token → 401
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from marvin_voice_core.puck_command_queue import PuckCommandQueue


def _make_vc():
    vc = MagicMock()
    vc.bot.cogs.get.return_value = None
    return vc


@pytest.mark.asyncio
async def test_car_control_404_when_not_wired():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    app = build_text_app(_make_vc(), token="s3cret")   # 無 puck_command_queue
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_control?cmd=stop&t=s3cret")
        assert resp.status == 404


@pytest.mark.asyncio
async def test_car_control_play_enqueues_command():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    q = PuckCommandQueue()
    app = build_text_app(_make_vc(), token="s3cret", puck_command_queue=q)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_control?cmd=play&url=https://youtu.be/a&t=s3cret")
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True
        assert body["cmd"] == "play"

    seq, pending = q.since(0)
    assert [c["cmd"] for c in pending] == ["play"]
    assert pending[0]["url"] == "https://youtu.be/a"


@pytest.mark.asyncio
async def test_car_control_play_missing_url_400():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    q = PuckCommandQueue()
    app = build_text_app(_make_vc(), token="s3cret", puck_command_queue=q)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_control?cmd=play&t=s3cret")
        assert resp.status == 400


@pytest.mark.asyncio
async def test_car_control_stop_enqueues_command():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    q = PuckCommandQueue()
    app = build_text_app(_make_vc(), token="s3cret", puck_command_queue=q)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_control?cmd=stop&t=s3cret")
        assert resp.status == 200

    seq, pending = q.since(0)
    assert [c["cmd"] for c in pending] == ["stop"]


@pytest.mark.asyncio
async def test_car_control_bad_cmd_400():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    q = PuckCommandQueue()
    app = build_text_app(_make_vc(), token="s3cret", puck_command_queue=q)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_control?cmd=teleport&t=s3cret")
        assert resp.status == 400


@pytest.mark.asyncio
async def test_car_control_token_gated():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    q = PuckCommandQueue()
    app = build_text_app(_make_vc(), token="s3cret", puck_command_queue=q)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_control?cmd=stop")   # 無 token
        assert resp.status == 401
