"""TDD: GPS 訊號進站 —— /car 心跳夾帶 lat/lon（隨身 ESP32 puck，唯一定位訊號源）。

DJ 播報只在車上（ESP32 puck）或家裡（Discord/Mac）播出，家裡直接用預設城市，
不需要另一條手機定位訊號，故只有 /car 這一個進站點。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_vc():
    vc = MagicMock()
    vc.handle_stt_result = AsyncMock()
    vc.bot.cogs.get.return_value = None
    return vc


@pytest.mark.asyncio
async def test_car_present_with_latlon_saves_location(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    from car_presence import CarPresence
    from location_state import load_location_state

    state_path = str(tmp_path / "location_state.json")
    cp = CarPresence(on_arrive=AsyncMock(), on_depart=AsyncMock())
    app = build_text_app(_make_vc(), token="s3cret", car_presence=cp,
                          location_state_path=state_path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/car?t=s3cret",
            json={"state": "present", "lat": 25.0693, "lon": 121.5885},
        )
        assert resp.status == 200

    state = load_location_state(path=state_path)
    assert state["lat"] == pytest.approx(25.0693)
    assert state["lon"] == pytest.approx(121.5885)
    assert state["ts"] is not None


@pytest.mark.asyncio
async def test_car_present_without_latlon_does_not_touch_location_state(tmp_path):
    """ESP32 puck 只有前 15 分鐘那次心跳帶座標，其餘心跳不帶——不該覆蓋成 None。"""
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    from car_presence import CarPresence
    from location_state import load_location_state

    state_path = str(tmp_path / "location_state.json")
    cp = CarPresence(on_arrive=AsyncMock(), on_depart=AsyncMock())
    app = build_text_app(_make_vc(), token="s3cret", car_presence=cp,
                          location_state_path=state_path)
    async with TestClient(TestServer(app)) as client:
        await client.post("/car?t=s3cret",
                           json={"state": "present", "lat": 25.0693, "lon": 121.5885})
        resp = await client.post("/car?t=s3cret", json={"state": "present"})
        assert resp.status == 200

    state = load_location_state(path=state_path)
    assert state["lat"] == pytest.approx(25.0693)  # 沒被沒帶座標的心跳蓋掉
