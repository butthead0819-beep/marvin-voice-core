"""
tests/test_car_presence.py
TDD：車載 presence 狀態機 + POST /car 端點（ESP32 puck）。

設計（design doc + eng review）：
- present 只在「到達」觸發開場一次；後續 heartbeat 只續期，不重觸發（debounce，外部聲音#8）。
- 熄火斷電 → puck 停送 heartbeat → TTL 逾時視為 absent（present 不 sticky）。
- absent / TTL 逾時 → 停播（MVP：不寫記憶）。
- /car 端點吃既有 token middleware（架構#1），無 token → 401。

純邏輯（注入時鐘 + callback）無網路；HTTP 層用 aiohttp TestServer。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock


def _clock():
    """可推進的假時鐘：回 (time_fn, advance)。"""
    now = [0.0]
    return (lambda: now[0]), (lambda dt: now.__setitem__(0, now[0] + dt))


# ── 純狀態機 ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_present_fires_arrive_once_heartbeat_debounced():
    from car_presence import CarPresence
    t, _adv = _clock()
    arrive, depart = AsyncMock(), AsyncMock()
    cp = CarPresence(on_arrive=arrive, on_depart=depart, ttl_s=90.0, time_fn=t)
    await cp.present()          # 到達
    await cp.present()          # heartbeat
    await cp.present()          # heartbeat
    arrive.assert_awaited_once()   # 只觸發一次開場，heartbeat 不重觸發
    assert cp.is_present is True


@pytest.mark.asyncio
async def test_absent_fires_depart():
    from car_presence import CarPresence
    t, _adv = _clock()
    arrive, depart = AsyncMock(), AsyncMock()
    cp = CarPresence(on_arrive=arrive, on_depart=depart, time_fn=t)
    await cp.present()
    await cp.absent()
    depart.assert_awaited_once()
    assert cp.is_present is False


@pytest.mark.asyncio
async def test_absent_without_present_is_noop():
    from car_presence import CarPresence
    t, _adv = _clock()
    arrive, depart = AsyncMock(), AsyncMock()
    cp = CarPresence(on_arrive=arrive, on_depart=depart, time_fn=t)
    await cp.absent()           # 從沒 present 過
    depart.assert_not_awaited()


@pytest.mark.asyncio
async def test_ttl_timeout_fires_depart():
    """熄火：puck 停送 heartbeat → 逾 TTL → 視為 absent、停播。"""
    from car_presence import CarPresence
    t, adv = _clock()
    arrive, depart = AsyncMock(), AsyncMock()
    cp = CarPresence(on_arrive=arrive, on_depart=depart, ttl_s=90.0, time_fn=t)
    await cp.present()
    adv(91.0)                   # 超過 TTL 沒 heartbeat
    fired = await cp.check_ttl()
    assert fired is True
    depart.assert_awaited_once()
    assert cp.is_present is False


@pytest.mark.asyncio
async def test_ttl_not_timeout_when_heartbeat_fresh():
    from car_presence import CarPresence
    t, adv = _clock()
    arrive, depart = AsyncMock(), AsyncMock()
    cp = CarPresence(on_arrive=arrive, on_depart=depart, ttl_s=90.0, time_fn=t)
    await cp.present()
    adv(60.0)
    await cp.present()          # heartbeat 續期
    adv(60.0)                   # 距上次 heartbeat 才 60s < 90s
    fired = await cp.check_ttl()
    assert fired is False
    depart.assert_not_awaited()
    assert cp.is_present is True


# ── HTTP /car 端點 ─────────────────────────────────────────────────────────
def _make_vc():
    vc = MagicMock()
    vc.handle_stt_result = AsyncMock()
    vc.bot.cogs.get.return_value = None
    return vc


@pytest.mark.asyncio
async def test_car_endpoint_present_triggers_arrive():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    from car_presence import CarPresence
    arrive, depart = AsyncMock(), AsyncMock()
    cp = CarPresence(on_arrive=arrive, on_depart=depart)
    app = build_text_app(_make_vc(), token="s3cret", car_presence=cp)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/car?t=s3cret", json={"state": "present"})
        assert resp.status == 200
        assert (await resp.json())["present"] is True
    arrive.assert_awaited_once()


@pytest.mark.asyncio
async def test_car_endpoint_absent_triggers_depart():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    from car_presence import CarPresence
    arrive, depart = AsyncMock(), AsyncMock()
    cp = CarPresence(on_arrive=arrive, on_depart=depart)
    app = build_text_app(_make_vc(), token="s3cret", car_presence=cp)
    async with TestClient(TestServer(app)) as client:
        await client.post("/car?t=s3cret", json={"state": "present"})
        resp = await client.post("/car?t=s3cret", json={"state": "absent"})
        assert resp.status == 200
    depart.assert_awaited_once()


@pytest.mark.asyncio
async def test_car_endpoint_token_gated():
    """新端點也吃 middleware：無 token → 401。"""
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    from car_presence import CarPresence
    cp = CarPresence(on_arrive=AsyncMock(), on_depart=AsyncMock())
    app = build_text_app(_make_vc(), token="s3cret", car_presence=cp)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/car", json={"state": "present"})   # 無 token
        assert resp.status == 401


@pytest.mark.asyncio
async def test_car_endpoint_bad_state_400():
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    from car_presence import CarPresence
    cp = CarPresence(on_arrive=AsyncMock(), on_depart=AsyncMock())
    app = build_text_app(_make_vc(), token="s3cret", car_presence=cp)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/car?t=s3cret", json={"state": "flying"})
        assert resp.status == 400


@pytest.mark.asyncio
async def test_car_endpoint_off_when_no_presence_wired():
    """車載模式沒接（car_presence=None）→ 400 car_mode_off，不炸。"""
    from aiohttp.test_utils import TestClient, TestServer
    from main_satellite import build_text_app
    app = build_text_app(_make_vc(), token="s3cret")   # 無 car_presence
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/car?t=s3cret", json={"state": "present"})
        assert resp.status == 400
        assert (await resp.json())["error"] == "car_mode_off"
