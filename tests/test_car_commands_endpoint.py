"""
tests/test_car_commands_endpoint.py

TDD：GET /car_commands — ESP32 edge端混音（MARVIN_CAR_HARDWARE=esp32_edge_mix）指令輪詢
端點（pull model，見 marvin_voice_core/puck_command_queue.py 開頭說明）。

驗：
(a) puck_command_queue=None（功能未開）→ 404
(b) 有指令 → 200 + JSON {"seq":..., "commands":[...]}，since 之後的指令都要拿到
(c) since 帶目前 seq → commands 為空（沒有新指令）
(d) 也吃既有 token middleware：無 token → 401
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
async def test_car_commands_404_when_not_wired():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    app = build_text_app(_make_vc(), token="s3cret")   # 無 puck_command_queue
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_commands?since=0&t=s3cret")
        assert resp.status == 404


@pytest.mark.asyncio
async def test_car_commands_returns_pending_commands():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    q = PuckCommandQueue()
    q.play("https://youtu.be/a")
    q.crossfade(duration_s=4.0)

    app = build_text_app(_make_vc(), token="s3cret", puck_command_queue=q)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_commands?since=0&t=s3cret")
        assert resp.status == 200
        body = await resp.json()
        assert body["seq"] == 2
        assert [c["cmd"] for c in body["commands"]] == ["play", "crossfade"]


@pytest.mark.asyncio
async def test_car_commands_since_current_seq_returns_empty():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    q = PuckCommandQueue()
    q.play("https://youtu.be/a")

    app = build_text_app(_make_vc(), token="s3cret", puck_command_queue=q)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_commands?since=1&t=s3cret")
        body = await resp.json()
        assert body["seq"] == 1
        assert body["commands"] == []


@pytest.mark.asyncio
async def test_car_commands_token_gated():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app

    q = PuckCommandQueue()
    app = build_text_app(_make_vc(), token="s3cret", puck_command_queue=q)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/car_commands?since=0")   # 無 token
        assert resp.status == 401
