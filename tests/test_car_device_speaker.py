"""
tests/test_car_device_speaker.py
TDD：車載裝置自帶身分（方案 A：同一電台、分身分）。

- parse_device_speakers / resolve_device_speaker：白名單解析（純函式）。
- POST /car、POST /audio：裝置送的 speaker 經白名單驗證，未知身分 400。
- inject_audio(speaker=...)：轉錄後用指定身分注入，不再固定讀環境變數。

HTTP 層用 aiohttp TestServer；inject_text / inject_audio 用 monkeypatch 換成 AsyncMock，
好直接看「傳進去的身分」。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


DEFAULT = "狗與露"


def _make_vc():
    vc = MagicMock()
    vc.handle_stt_result = AsyncMock()
    vc.bot.cogs.get.return_value = None
    return vc


# ── parse_device_speakers ───────────────────────────────────────────────────
def test_parse_device_speakers_strips_and_skips_blanks():
    from car_http_app import parse_device_speakers
    out = parse_device_speakers("狗與露, showay ,,", DEFAULT)
    assert out == {"狗與露": "狗與露", "showay": "showay"}


def test_parse_device_speakers_always_includes_default():
    from car_http_app import parse_device_speakers
    out = parse_device_speakers("showay", DEFAULT)
    assert out == {"狗與露": "狗與露", "showay": "showay"}


def test_parse_device_speakers_keys_are_casefolded():
    from car_http_app import parse_device_speakers
    out = parse_device_speakers("ShowAy", DEFAULT)
    assert out["showay"] == "ShowAy"


# ── resolve_device_speaker ──────────────────────────────────────────────────
def test_resolve_allowed_none_ignores_device_value():
    from car_http_app import resolve_device_speaker
    assert resolve_device_speaker("showay", None, DEFAULT) == DEFAULT


@pytest.mark.parametrize("raw", [None, "  "])
def test_resolve_missing_or_blank_falls_back_to_default(raw):
    from car_http_app import parse_device_speakers, resolve_device_speaker
    allowed = parse_device_speakers("showay", DEFAULT)
    assert resolve_device_speaker(raw, allowed, DEFAULT) == DEFAULT


def test_resolve_case_insensitive_returns_formal_name():
    from car_http_app import parse_device_speakers, resolve_device_speaker
    allowed = parse_device_speakers("showay", DEFAULT)
    assert resolve_device_speaker("Showay", allowed, DEFAULT) == "showay"


def test_resolve_unknown_returns_none():
    from car_http_app import parse_device_speakers, resolve_device_speaker
    allowed = parse_device_speakers("showay", DEFAULT)
    assert resolve_device_speaker("eve", allowed, DEFAULT) is None


# ── POST /car ───────────────────────────────────────────────────────────────
def _car_app(cp, device_speakers):
    from car_http_app import build_text_app, parse_device_speakers
    allowed = parse_device_speakers(device_speakers, DEFAULT)
    return build_text_app(_make_vc(), token="s3cret", car_presence=cp,
                          default_speaker=DEFAULT, device_speakers=allowed)


@pytest.mark.asyncio
async def test_car_present_with_speaker_registers_that_occupant():
    from aiohttp.test_utils import TestClient, TestServer
    from car_presence import CarPresence
    arrive, depart = AsyncMock(), AsyncMock()
    cp = CarPresence(on_arrive=arrive, on_depart=depart)
    app = _car_app(cp, "showay")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/car?t=s3cret",
                                 json={"state": "present", "speaker": "showay"})
        assert resp.status == 200
        body = await resp.json()
    assert body["occupants"] == ["showay"]
    assert body["present"] is True
    arrive.assert_awaited_once_with("showay")


@pytest.mark.asyncio
async def test_car_present_without_speaker_uses_default():
    from aiohttp.test_utils import TestClient, TestServer
    from car_presence import CarPresence
    arrive = AsyncMock()
    cp = CarPresence(on_arrive=arrive, on_depart=AsyncMock())
    app = _car_app(cp, "showay")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/car?t=s3cret", json={"state": "present"})
        assert resp.status == 200
    arrive.assert_awaited_once_with(DEFAULT)


@pytest.mark.asyncio
async def test_car_unknown_speaker_400_and_presence_untouched():
    from aiohttp.test_utils import TestClient, TestServer
    from car_presence import CarPresence
    arrive = AsyncMock()
    cp = CarPresence(on_arrive=arrive, on_depart=AsyncMock())
    app = _car_app(cp, "showay")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/car?t=s3cret",
                                 json={"state": "present", "speaker": "eve"})
        assert resp.status == 400
        assert (await resp.json())["error"] == "unknown_speaker"
    arrive.assert_not_awaited()
    assert cp.is_present is False
    assert cp.occupants == []


@pytest.mark.asyncio
async def test_car_present_query_string_speaker():
    """非 JSON（query string）也要帶身分。"""
    from aiohttp.test_utils import TestClient, TestServer
    from car_presence import CarPresence
    cp = CarPresence(on_arrive=AsyncMock(), on_depart=AsyncMock())
    app = _car_app(cp, "showay")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/car?t=s3cret&state=present&speaker=showay")
        assert resp.status == 200
        assert (await resp.json())["occupants"] == ["showay"]


# ── POST /audio ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_audio_passes_device_speaker_to_inject(monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    import car_http_app
    fake = AsyncMock(return_value=True)
    monkeypatch.setattr(car_http_app, "inject_audio", fake)
    app = _car_app(None, "showay")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/audio?t=s3cret&speaker=showay", data=b"RIFFfake")
        assert resp.status == 200
    fake.assert_awaited_once()
    assert fake.call_args.kwargs["speaker"] == "showay"


@pytest.mark.asyncio
async def test_audio_trigger_pause_passes_require_wake(monkeypatch):
    """車機暫停鍵觸發（?trigger=pause）要以 require_wake=True 呼叫 inject_audio。"""
    from aiohttp.test_utils import TestClient, TestServer
    import car_http_app
    fake = AsyncMock(return_value=True)
    monkeypatch.setattr(car_http_app, "inject_audio", fake)
    app = _car_app(None, "showay")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/audio?t=s3cret&speaker=showay&trigger=pause", data=b"RIFFfake")
        assert resp.status == 200
    fake.assert_awaited_once()
    assert fake.call_args.kwargs["require_wake"] is True


@pytest.mark.asyncio
async def test_audio_without_trigger_does_not_require_wake(monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    import car_http_app
    fake = AsyncMock(return_value=True)
    monkeypatch.setattr(car_http_app, "inject_audio", fake)
    app = _car_app(None, "showay")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/audio?t=s3cret&speaker=showay", data=b"RIFFfake")
        assert resp.status == 200
    fake.assert_awaited_once()
    assert fake.call_args.kwargs["require_wake"] is False


@pytest.mark.asyncio
async def test_audio_unknown_speaker_400_and_no_inject(monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    import car_http_app
    fake = AsyncMock(return_value=True)
    monkeypatch.setattr(car_http_app, "inject_audio", fake)
    app = _car_app(None, "showay")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/audio?t=s3cret&speaker=eve", data=b"RIFFfake")
        assert resp.status == 400
        assert (await resp.json())["error"] == "unknown_speaker"
    fake.assert_not_awaited()


# ── inject_audio(speaker=...) ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_inject_audio_uses_given_speaker(monkeypatch):
    import car_http_app
    fake_inject_text = AsyncMock(return_value=True)
    monkeypatch.setattr(car_http_app, "inject_text", fake_inject_text)
    monkeypatch.setenv("MARVIN_SATELLITE_SPEAKER", DEFAULT)
    vc = MagicMock()
    vc.bot.engine._run_swift_stt = AsyncMock(return_value=("現在幾點", {}))
    ok = await car_http_app.inject_audio(vc, b"RIFFfake", speaker="showay")
    assert ok is True
    assert fake_inject_text.call_args.args[1] == "showay"


@pytest.mark.asyncio
async def test_inject_audio_without_speaker_falls_back_to_env(monkeypatch):
    import car_http_app
    fake_inject_text = AsyncMock(return_value=True)
    monkeypatch.setattr(car_http_app, "inject_text", fake_inject_text)
    monkeypatch.setenv("MARVIN_SATELLITE_SPEAKER", "阿凱")
    vc = MagicMock()
    vc.bot.engine._run_swift_stt = AsyncMock(return_value=("現在幾點", {}))
    await car_http_app.inject_audio(vc, b"RIFFfake")
    assert fake_inject_text.call_args.args[1] == "阿凱"


# ── parse_car_devices ────────────────────────────────────────────────────────
def test_parse_car_devices_two_items():
    from car_http_app import parse_car_devices
    out = parse_car_devices("puck-aa1122:狗與露,puck-bb3344:showay")
    assert out == {"puck-aa1122": "狗與露", "puck-bb3344": "showay"}


def test_parse_car_devices_strips_whitespace():
    from car_http_app import parse_car_devices
    out = parse_car_devices(" puck-aa1122 : 狗與露 , puck-bb3344 : showay ")
    assert out == {"puck-aa1122": "狗與露", "puck-bb3344": "showay"}


def test_parse_car_devices_id_is_casefolded():
    from car_http_app import parse_car_devices
    out = parse_car_devices("PUCK-A1B2C3:狗與露")
    assert out == {"puck-a1b2c3": "狗與露"}


def test_parse_car_devices_skips_missing_colon_or_empty_id_or_name():
    from car_http_app import parse_car_devices
    out = parse_car_devices("noColonHere,:showay,puck-x:,puck-ok:name")
    assert out == {"puck-ok": "name"}


def test_parse_car_devices_name_may_contain_colon():
    from car_http_app import parse_car_devices
    out = parse_car_devices("p1:a:b")
    assert out == {"p1": "a:b"}


def test_parse_car_devices_duplicate_id_keeps_first():
    from car_http_app import parse_car_devices
    out = parse_car_devices("p1:first,p1:second")
    assert out == {"p1": "first"}


def test_parse_car_devices_empty_string():
    from car_http_app import parse_car_devices
    assert parse_car_devices("") == {}


# ── resolve_car_identity ─────────────────────────────────────────────────────
def test_resolve_car_identity_registered_device_returns_name():
    from car_http_app import parse_car_devices, resolve_car_identity
    devices = parse_car_devices("puck-aa1122:狗與露")
    assert resolve_car_identity("PUCK-AA1122", None, devices, None, DEFAULT) == ("狗與露", None)


def test_resolve_car_identity_registered_device_ignores_speaker():
    from car_http_app import parse_car_devices, parse_device_speakers, resolve_car_identity
    devices = parse_car_devices("puck-aa1122:狗與露")
    allowed = parse_device_speakers("showay", DEFAULT)
    assert resolve_car_identity("puck-aa1122", "showay", devices, allowed, DEFAULT) == ("狗與露", None)


def test_resolve_car_identity_unregistered_device_is_unknown_device():
    from car_http_app import parse_car_devices, resolve_car_identity
    devices = parse_car_devices("puck-aa1122:狗與露")
    assert resolve_car_identity("puck-zzzz", None, devices, None, DEFAULT) == (None, "unknown_device")


def test_resolve_car_identity_device_present_but_car_devices_none():
    from car_http_app import resolve_car_identity
    assert resolve_car_identity("puck-aa1122", None, None, None, DEFAULT) == (None, "unknown_device")


def test_resolve_car_identity_no_device_falls_back_to_speaker_default():
    from car_http_app import resolve_car_identity
    assert resolve_car_identity(None, None, {"puck-aa1122": "狗與露"}, None, DEFAULT) == (DEFAULT, None)


def test_resolve_car_identity_blank_device_falls_back_to_speaker_logic():
    from car_http_app import parse_device_speakers, resolve_car_identity
    allowed = parse_device_speakers("showay", DEFAULT)
    assert resolve_car_identity("   ", "showay", {"puck-aa1122": "狗與露"}, allowed, DEFAULT) == ("showay", None)


def test_resolve_car_identity_no_device_unknown_speaker():
    from car_http_app import parse_device_speakers, resolve_car_identity
    allowed = parse_device_speakers("showay", DEFAULT)
    assert resolve_car_identity(None, "eve", None, allowed, DEFAULT) == (None, "unknown_speaker")


# ── POST /car with device ────────────────────────────────────────────────────
def _car_app_with_devices(cp, car_devices_raw, device_speakers_raw=""):
    from car_http_app import build_text_app, parse_car_devices, parse_device_speakers
    car_devices = parse_car_devices(car_devices_raw)
    allowed = parse_device_speakers(device_speakers_raw, DEFAULT)
    return build_text_app(_make_vc(), token="s3cret", car_presence=cp,
                          default_speaker=DEFAULT, device_speakers=allowed,
                          car_devices=car_devices)


@pytest.mark.asyncio
async def test_car_present_with_registered_device_registers_that_occupant():
    from aiohttp.test_utils import TestClient, TestServer
    from car_presence import CarPresence
    arrive = AsyncMock()
    cp = CarPresence(on_arrive=arrive, on_depart=AsyncMock())
    app = _car_app_with_devices(cp, "puck-aa1122:狗與露")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/car?t=s3cret",
                                 json={"state": "present", "device": "puck-aa1122"})
        assert resp.status == 200
        body = await resp.json()
    assert body["occupants"] == ["狗與露"]
    arrive.assert_awaited_once_with("狗與露")


@pytest.mark.asyncio
async def test_car_present_with_unregistered_device_400_unknown_device():
    from aiohttp.test_utils import TestClient, TestServer
    from car_presence import CarPresence
    arrive = AsyncMock()
    cp = CarPresence(on_arrive=arrive, on_depart=AsyncMock())
    app = _car_app_with_devices(cp, "puck-aa1122:狗與露")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/car?t=s3cret",
                                 json={"state": "present", "device": "puck-zzzz"})
        assert resp.status == 400
        assert (await resp.json())["error"] == "unknown_device"
    arrive.assert_not_awaited()
    assert cp.is_present is False


# ── POST /audio with device ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_audio_with_registered_device_passes_name(monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    import car_http_app
    fake = AsyncMock(return_value=True)
    monkeypatch.setattr(car_http_app, "inject_audio", fake)
    app = _car_app_with_devices(None, "puck-aa1122:狗與露")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/audio?t=s3cret&device=puck-aa1122", data=b"RIFFfake")
        assert resp.status == 200
    fake.assert_awaited_once()
    assert fake.call_args.kwargs["speaker"] == "狗與露"


@pytest.mark.asyncio
async def test_audio_with_unregistered_device_400_unknown_device(monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    import car_http_app
    fake = AsyncMock(return_value=True)
    monkeypatch.setattr(car_http_app, "inject_audio", fake)
    app = _car_app_with_devices(None, "puck-aa1122:狗與露")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/audio?t=s3cret&device=puck-zzzz", data=b"RIFFfake")
        assert resp.status == 400
        assert (await resp.json())["error"] == "unknown_device"
    fake.assert_not_awaited()
